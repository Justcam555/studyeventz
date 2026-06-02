# How to Download Source Data Files

## Step 1 — AEI Commencements Data (one file per country)

**URL:** https://www.education.gov.au/international-education-data-and-research/international-student-monthly-summary-and-data-tables

1. Open the page above in your browser
2. Click into the **Tableau dashboard** (the interactive chart/pivot)
3. Look for a **"Nationality"** filter — set it to **Thailand**
4. Click **Download → Data → Crosstab** (or similar export button in the Tableau toolbar)
5. Save the file as:
   ```
   data/raw/aei_thailand_2025-12.xlsx
   ```
   (adjust the month to match whatever the latest data period is)
6. Repeat for **Nepal** → save as `data/raw/aei_nepal_2025-12.xlsx`

> The downloaded file must have sheet `AEI_Pivot` with the nationality filter set.
> Downloading without setting the filter gives All Nationalities data — not useful.

---

## Step 2 — Home Affairs Visa Grants

**URL:** https://data.gov.au/data/dataset/student-visas

1. Open the page above
2. Find the most recent Excel file in the **"Student visa program"** dataset
3. Download it and save as:
   ```
   data/raw/visa_grants_latest.xlsx
   ```

---

## Step 3 — Run the pipeline

```bash
cd ~/Desktop/Agent\ Scraper/market_intel

# Process AEI + Home Affairs into JSON
python3 process_market_data.py

# Build HTML preview blocks
python3 build_market_blocks.py
```

Output files:
- `data/processed/market_size_YYYY-MM.json` — structured data for display layer
- `data/processed/market_blocks_YYYY-MM.html` — standalone HTML preview
- `data/processed/market_snippets_YYYY-MM.json` — per-country HTML snippets

---

## AEI Nationality Name Spellings

If a country isn't matching, check the exact spelling used in the AEI filter:

| Database name | AEI filter spelling |
|---|---|
| Vietnam | Viet Nam |
| China | China (People's Republic of) |
| Hong Kong | Hong Kong (SAR of China) |
| South Korea | Korea, Republic of |
| All others | Same as standard English name |
