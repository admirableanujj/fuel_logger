# 🛢️ Fuel Logger to Google Sheets

This app takes receipt text from gas stations, extracts key details, and logs them to a connected Google Sheet. It also calculates mileage based on odometer readings.

## 🚀 Features
- Text input parser (invoice, date, time, gallons, price, address)
- Google Sheets integration
- Auto mileage calculation
- Streamlit UI (optional)

## 📦 Setup

### 1. Clone the Repo
```bash
git clone https://github.com/yourusername/fuel-logger-app.git
cd fuel-logger-app
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Google API Setup
- Enable Sheets API in [Google Cloud Console](https://console.cloud.google.com/)
- Create a Service Account and download your `service_account.json`
- Place it in the `creds/` folder
- Share your target Google Sheet with the service account email

### 4. Run the App
```bash
streamlit run app.py
```

## 🧪 Example Input

See `test_data/sample_receipt.txt`.

## 🛡️ Security Note
Never commit your `service_account.json` or sensitive credentials.
