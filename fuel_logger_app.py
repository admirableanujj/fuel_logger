import os
from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename
from PIL import Image
import pytesseract
import easyocr
import gspread
import re
import io
import pandas as pd
import logging
import gspread
import streamlit as st
from datetime import datetime, timedelta

# Configure logging for the Flask app
logging.basicConfig(level=logging.INFO)
# Define Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png'}
app.secret_key = 'supersecretkey' #AJS change this key to a strong, random key in production

# Ensure the upload folder exists

# ============================================================================== #
# 1. DATA CLASS
# ============================================================================== #
class Receipt:
    """A class to hold all data related to a single receipt."""
    def __init__(self, image_bytes):
        self.image_bytes = image_bytes
        self.raw_text = ""
        self.parsed_data = {}

# ============================================================================== #
# 2. OCR PROCESSOR CLASSES
# ============================================================================== #
class OCRProcessor:
    """Abstract Base Class for an OCR Processor."""
    def process(self, image_bytes: bytes) -> str:
        raise NotImplementedError

class TesseractProcessor(OCRProcessor):
    """OCR Processor that uses Tesseract."""
    def process(self, image_bytes: bytes) -> str:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            return pytesseract.image_to_string(image, lang='eng')
        except pytesseract.TesseractNotFoundError:
            app.logger.error("Tesseract is not installed or not in your PATH. Please install it.")
            return "Tesseract is not installed or not in your PATH."
        except Exception as e:
            app.logger.error(f"Tesseract Error: {e}")
            return f"Tesseract Error: {e}"

class EasyOCRProcessor(OCRProcessor):
    """OCR Processor that uses EasyOCR."""
    def __init__(self):
        # Initialize EasyOCR reader once to avoid re-loading models on every request        
        self.reader = easyocr.Reader(['en'])

    def process(self, image_bytes: bytes) -> str:
        try:
            # EasyOCR can directly take image bytes            
            results = self.reader.readtext(image_bytes)
            return " ".join([res[1] for res in results])
        except Exception as e:
            app.logger.error(f"EasyOCR Error: {e}")            
            return f"EasyOCR Error: {e}"

# ============================================================================== #
# 3. PARSER CLASS
# ============================================================================== #
class ReceiptParser:
    """Encapsulates the logic to parse raw text from a receipt."""
    def __init__(self):
        self.patterns = {
            # Date patterns (e.g., 01/01/2023, 01-01-2023)
            "date": re.compile(r'Date[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', re.IGNORECASE),
            # Total patterns (e.g., Total $12.34, Total Sale 12.34)
            "total": re.compile(r'(?:Total|Total Sale|Amount Due|Total\s+Sale\s+S)[:\s]*\$?(\d+\.\d{2})', re.IGNORECASE),
            "total_sale": re.compile(r'Total\s+Sale\s+S\s+(\d+\s*\.\s*\d{2})', re.IGNORECASE),
            # Gallons patterns
            "gallons": re.compile(r'Gallons[:\s]*([\d\.]+)', re.IGNORECASE),
            "gallons_a": re.compile(r'Gallons\s*([\d\.]+)', re.IGNORECASE),
            # Price per gallon patterns (e.g., $3.459/G, Price/Gallon 3.459)
            "price_per_gallon": re.compile(r'(?:Price per Gallon|Price/Gallon|Price)[:\s]*\$?([\d\.]+)', re.IGNORECASE),
            # Invoice Number patterns
            "invoice_number": re.compile(r'(?:Invoice|Inuoice)[:#\s]*([A-Za-z0-9]+)', re.IGNORECASE),
            # Time patterns (e.g., 10:30, 10:30 AM)
            "time": re.compile(r'Time[:\s]*(\d{1,2}:\d{2}(?:\s*[APap][Mm])?)', re.IGNORECASE),
            # Address patterns (simple example, can be more complex)
            "address": re.compile(r'Address[:\s]*([\w\s,.-]+(?:\s*\d{5})?)', re.IGNORECASE),
            # Odometer patterns
            "odometer": re.compile(r'Odometer[:\s]*([\d]+)', re.IGNORECASE)
        }


    def parse(self, text: str) -> dict:
        """
        Parses the raw text from a receipt to extract structured data.
        Uses a dictionary of regex patterns to find relevant information.
        """
        data = {}

        # Extract fields using the defined patterns
        data["Date"] = self._find_match(self.patterns["date"], text)
        data["Total"] = self._find_match(self.patterns["total"], text, cast_type=float)
        data["Gallons"] = self._find_match(self.patterns["gallons"], text, cast_type=float)
        data["total_sale"] = self._find_match(self.patterns["total_sale"], text, cast_type=float)
        data["gallons_a"] = self.patterns["gallons_a"].search(text)
        data["Price_per_Gallon"] = self._find_match(self.patterns["price_per_gallon"], text, cast_type=float)
        data["Invoice_Number"] = self._find_match(self.patterns["invoice_number"], text)
        data["Time"] = self._find_match(self.patterns["time"], text)
        data["Address"] = self._find_match(self.patterns["address"], text)
        data["Odometer"] = self._find_match(self.patterns["odometer"], text, cast_type=int)
        print(data)
        # Calculate Price_per_Gallon if Total and Gallons are found but Price_per_Gallon isn't
        if data.get("Total") is not None and data.get("Gallons") is not None and data.get("Gallons") > 0 and data.get("Price_per_Gallon") is None:
            try:
                data["Price_per_Gallon"] = round(data["Total"] / data["Gallons"], 3)
            except ZeroDivisionError:
                data["Price_per_Gallon"] = None

        return data

    def _find_match(self, pattern, text, group=1, cast_type=str):
        """
        Helper method to search for a pattern and return the captured group.
        """
        match = pattern.search(text)
        if match:
            try:
                value = match.group(group)
                return cast_type(value)
            except (ValueError, IndexError):
                return None
        return None

# ============================================================================== #
# 4. GOOGLE SHEET LOGGER CLASS
# ============================================================================== #
class GoogleSheetLogger:
    """Handles all communication with the Google Sheets API."""
    def __init__(self, credentials_path: str, sheet_name: str):
        self.credentials_path = credentials_path
        self.sheet_name = sheet_name
        self.worksheet = self._connect()

    # def _connect(self):
    #     try:
    #         gc = gspread.service_account(filename=self.credentials_path)
    #         return gc.open(self.sheet_name).sheet1
    #     except Exception as e:
    #         return f"Error: {e}"

    def _connect(self):
        try:
            gc = gspread.service_account(filename=self.credentials_path)
            # Ensure the connection returns a valid worksheet object
            sheet = gc.open(self.sheet_name)
            worksheet = sheet.get_worksheet(0)  # Access the first sheet (0-indexed)
            app.logger.info(f"Successfully connected to Google Sheet: {self.sheet_name}")
            return worksheet
        except FileNotFoundError:
            app.logger.error(f"Credentials file not found at '{self.credentials_path}'.")            
            st.error(f"Credentials file not found at '{self.credentials_path}'.")
            return None
        except gspread.exceptions.SpreadsheetNotFound:
            app.logger.error(f"Spreadsheet named '{self.sheet_name}' not found or not shared with the service account.")            
            st.error(f"Spreadsheet named '{self.sheet_name}' not found or not shared.")
            return None
        except Exception as e:
            app.logger.error(f"Could not connect to Google Sheets: {e}")
            st.error(f"Could not connect to Google Sheets: {e}")
            return None

    def log(self, data: dict):
        if not self.worksheet:
            return "Google Sheet connection failed."
        try:
            time_now  = datetime.now()
            row_to_add = [
                str(time_now),
                data.get("Date", ""),
                data.get("Total", ""),
                data.get("Gallons", ""),
                data.get("Price_per_Gallon", ""),
                data.get("Invoice_Number", ""),
                data.get("Time", ""),
                data.get("Address", ""),
                data.get("Odometer", "")
            ]
            self.worksheet.append_row(row_to_add, value_input_option='USER_ENTERED')
            time_now = time_now.replace(microsecond=0)
            return f"Data successfully added to Google Sheet!  {time_now}"
        except Exception as e:
            app.logger.error(f"Failed to append row to Google Sheet: {e}")            
            return f"Failed to append row: {e}"

# ============================================================================== #
# Flask Routes
# ============================================================================== #
@app.route("/test")
def test():
    return render_template("index.html", receipt=None, log_message=None)

@app.route("/", methods=["GET", "POST"])
def index():
    """
    Main route for the application. Handles file upload, OCR processing,
    data parsing, and optional Google Sheet logging.
    """    
    if request.method == "POST":
        if 'receipt' not in request.files:
            flash("No file part in the request.", "error")
            return redirect(request.url)
        
        file = request.files['receipt']
        
        if file.filename == '':
            flash("No selected file")
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)# Save the uploaded file temporarily
            
            with open(file_path, 'rb') as f:
                image_bytes = f.read()
            
            # OCR Engine Selection
            ocr_engine_choice = request.form.get("ocr_engine")
            if ocr_engine_choice == "EasyOCR":
                ocr_processor = EasyOCRProcessor()
            else:
                ocr_processor = TesseractProcessor()

            # Process the image
            receipt = Receipt(image_bytes)
            if ocr_processor:
                receipt.raw_text = ocr_processor.process(receipt.image_bytes)
                if "Error:" in receipt.raw_text: # Check for error messages from OCR
                        flash(receipt.raw_text, "error")
                        receipt.raw_text = "OCR failed to extract text." # Clear raw text if error
                else:
                        flash("OCR processing complete!", "success")
            else:
                flash("No OCR processor selected.", "error")
                receipt.raw_text = "OCR processing skipped."            

            # Parse the receipt text
            parser = ReceiptParser()
            receipt.parsed_data = parser.parse(receipt.raw_text)
            flash("Data parsing complete!", "success")
            # d = parser.extract_fields(receipt.raw_text)
            # print(d)

            # Log the data to Google Sheets (set to True to enable logging)
            log_to_google_sheet = False # Set this to True to enable Google Sheet logging
            log_message = 'Google Sheet logging is currently disabled.'
            if log_to_google_sheet:
                logger = GoogleSheetLogger(credentials_path='credentials.json', sheet_name='my-fuel-log')
                log_message = logger.log(receipt.parsed_data)
                if "successfully" in log_message:
                    flash(log_message, "success")
                else:
                    flash(log_message, "error")
            else:
                flash(log_message, "warning")
                # app.logger.info(logger)

            # Clean up the uploaded file
            # os.remove(file_path)
            
            return render_template("index.html", receipt=receipt, log_message=log_message)

    return render_template("index.html")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

if __name__ == "__main__":
    # Run the Flask application
    # For development, use debug=True for auto-reloading and better error messages
    # For production, keep debug=False
    app.run(debug=False)
