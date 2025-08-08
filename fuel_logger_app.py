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

# Define Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png'}
app.secret_key = 'supersecretkey'

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
            return pytesseract.image_to_string(image)
        except pytesseract.TesseractNotFoundError:
            return "Tesseract is not installed or not in your PATH."
        except Exception as e:
            return f"Tesseract Error: {e}"

class EasyOCRProcessor(OCRProcessor):
    """OCR Processor that uses EasyOCR."""
    def __init__(self):
        self.reader = easyocr.Reader(['en'])

    def process(self, image_bytes: bytes) -> str:
        try:
            results = self.reader.readtext(image_bytes)
            return " ".join([res[1] for res in results])
        except Exception as e:
            return f"EasyOCR Error: {e}"

# ============================================================================== #
# 3. PARSER CLASS
# ============================================================================== #
class ReceiptParser:
    """Encapsulates the logic to parse raw text from a receipt."""
    def __init__(self):
        self.patterns = {
            "date": re.compile(r'Date[:\s]*([\d/]+)'),
            "total": re.compile(r'Total Sale\s*\$?\s*(\d+\.\d{2})'),
            "gallons": re.compile(r'Gallons[:\s]*([\d\.]+)'),
            "price_per_gallon": re.compile(r'\$\s*(\d\.\d{2,3})'),
            "time": re.compile(r'Time[:\s]*([\d:]+)')
        }

    def extract_fields(self, text: str) -> dict:
        data = {}
        data['Invoice Number'] = re.search(r'Invoice[:#\s]*([0-9]+)', text).group(1)
        data['Price per Gallon'] = float(re.search(r'Price[:/Gallon\s]*\$?([\d\.]+)', text).group(1))
        data['Gallons'] = float(re.search(r'Gallons[:\s]*([\d\.]+)', text).group(1))
        data['Date'] = re.search(r'Date[:\s]*([\d/]+)', text).group(1)
        data['Time'] = re.search(r'Time[:\s]*([\d:]+)', text).group(1)
        data['Address'] = re.search(r'Address[:\s]*(.+)', text).group(1).strip()
        data['Odometer'] = int(re.search(r'Odometer[:\s]*([\d]+)', text).group(1)) if 'Odometer' in text else None
        return data

    def parse(self, text: str) -> dict:
        data = {}
        data["Date"] = self._find_match(self.patterns["date"], text)
        data["Total"] = self._find_match(self.patterns["total"], text, group=1, cast_type=float)
        data["Gallons"] = self._find_match(self.patterns["gallons"], text, group=1, cast_type=float)
        price = self._find_match(self.patterns["price_per_gallon"], text, group=1, cast_type=float)
        if price:
            data["Price_per_Gallon"] = price
        elif data.get("Total") and data.get("Gallons"):
            data["Price_per_Gallon"] = round(data["Total"] / data["Gallons"], 3)
        else:
            data["Price_per_Gallon"] = None
        return data

    def _find_match(self, pattern, text, group=0, cast_type=str):
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
            return worksheet
        except FileNotFoundError:
            st.error(f"Credentials file not found at '{self.credentials_path}'.")
            return None
        except gspread.exceptions.SpreadsheetNotFound:
            st.error(f"Spreadsheet named '{self.sheet_name}' not found or not shared.")
            return None
        except Exception as e:
            st.error(f"Could not connect to Google Sheets: {e}")
            return None

    def log(self, data: dict):
        if not self.worksheet:
            return "Google Sheet connection failed."
        try:
            row_to_add = [
                data.get("Date", ""),
                data.get("Total", ""),
                data.get("Gallons", ""),
                data.get("Price_per_Gallon", "")
            ]
            self.worksheet.append_row(row_to_add, value_input_option='USER_ENTERED')
            return "Data successfully added to Google Sheet!"
        except Exception as e:
            return f"Failed to append row: {e}"

# ============================================================================== #
# Flask Routes
# ============================================================================== #
@app.route("/test")
def test():
    return render_template("index.html", receipt=None, log_message=None)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if 'receipt' not in request.files:
            flash("No file part")
            return redirect(request.url)
        file = request.files['receipt']
        if file.filename == '':
            flash("No selected file")
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
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
            receipt.raw_text = ocr_processor.process(receipt.image_bytes)

            # Parse the receipt text
            parser = ReceiptParser()
            receipt.parsed_data = parser.parse(receipt.raw_text)
            # d = parser.extract_fields(receipt.raw_text)
            # print(d)

            # Log the data to Google Sheets
            #change flag to logdata to Google Sheets
            log_message = 'Skipped Logging'
            if False:
                logger = GoogleSheetLogger(credentials_path='credentials.json', sheet_name='my-fuel-log')
                log_message = logger.log(receipt.parsed_data)
                app.logger.info(logger)

            return render_template("index.html", receipt=receipt, log_message=log_message)

    return render_template("index.html")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

if __name__ == "__main__":
    app.run(debug=False)
