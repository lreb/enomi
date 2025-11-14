import PyPDF2
import re
import io
import time
from pathlib import Path
from typing import Dict, List, Optional
import shutil
from datetime import datetime

"""
PDF Reader for Invoice and Bank Statement Processing
Extracts total amounts from invoices and transactions from bank statements
"""


# OCR configuration (used on malformed or image-only PDFs)
# On Windows, install Poppler and set this to its bin path, e.g. r"C:\\tools\\poppler\\Library\\bin"
POPPLER_PATH: Optional[str] = None
ENABLE_OCR_FALLBACK: bool = True
DEBUG_PARSE_TRANSACTIONS: bool = False



def read_pdf_text(pdf_path: str) -> str:
    """
    Extract all text content from a PDF file.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Extracted text content as a single string
    """
    text_content = ""

    # Load file bytes into memory to ensure no open file handles while processing/moving later
    with open(pdf_path, 'rb') as f:
        data = f.read()

    # Build reader from in-memory bytes to avoid locking the file on disk
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(data))
        # Extract text page-by-page, with per-page OCR fallback on failure
        for page_num in range(len(pdf_reader.pages)):
            try:
                page = pdf_reader.pages[page_num]
                text_content += (page.extract_text() or "") + "\n"
            except Exception as e:
                print(f"Warning: unreadable page {page_num + 1} in {pdf_path}: {e}")
                # Try OCR for this specific page if enabled and OCR deps available
                if ENABLE_OCR_FALLBACK:
                    try:
                        from pdf2image import convert_from_bytes
                        import pytesseract
                        images = convert_from_bytes(data, dpi=200, first_page=page_num + 1, last_page=page_num + 1, poppler_path=POPPLER_PATH)
                        if images:
                            ocr_text = pytesseract.image_to_string(images[0], lang='spa+eng')
                            if ocr_text and ocr_text.strip():
                                text_content += ocr_text + "\n"
                                print(f"OCR: recovered text from page {page_num + 1}")
                                continue
                    except Exception as oe:
                        print(f"OCR: failed to recover page {page_num + 1}: {oe}")
                # If OCR disabled or failed, continue to next page
                continue
    except Exception as e:
        print(f"Error opening PDF {pdf_path}: {e}")
        text_content = ""

    # If overall text is empty or too short, try full-document OCR fallback (optional)
    if ENABLE_OCR_FALLBACK and len(text_content.strip()) < 10:
        ocr_text = ocr_extract_text_from_pdf_bytes(data)
        if ocr_text:
            text_content = ocr_text

    return text_content


def ocr_extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """OCR fallback using pdf2image + pytesseract. Returns empty string if unavailable.

    On Windows, requires Poppler installed; set POPPLER_PATH above.
    """
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except Exception as e:
        # OCR not available
        print(f"OCR fallback unavailable (install pdf2image, pillow, pytesseract): {e}")
        return ""

    try:
        images = convert_from_bytes(pdf_bytes, dpi=200, poppler_path=POPPLER_PATH)
    except Exception as e:
        print(f"OCR: failed to render PDF pages: {e}")
        return ""

    text_parts: List[str] = []
    for idx, img in enumerate(images, start=1):
        try:
            # OCR in Spanish + English to improve matches
            text_parts.append(pytesseract.image_to_string(img, lang='spa+eng'))
        except Exception as e:
            print(f"OCR: failed on page {idx}: {e}")
            continue
    return "\n".join(text_parts).strip()


def extract_invoice_total(text: str) -> Optional[float]:
    """
    Extract total amount from invoice text.
    
    Args:
        text: Extracted PDF text content
        
    Returns:
        Total amount as float, or None if not found
    """
    # Common patterns for invoice totals
    patterns = [
        r'total[:\s]+\$?\s*(\d+[,\d]*\.?\d*)',
        r'amount due[:\s]+\$?\s*(\d+[,\d]*\.?\d*)',
        r'grand total[:\s]+\$?\s*(\d+[,\d]*\.?\d*)',
        r'balance due[:\s]+\$?\s*(\d+[,\d]*\.?\d*)',
    ]
    
    text_lower = text.lower()
    
    for pattern in patterns:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(',', '')
            return float(amount_str)
    
    return None


def extract_bank_transactions(text: str) -> List[Dict[str, str]]:
    """
    Extract transaction details from bank statement text.
    - Allows digits in descriptions (e.g., "7-Eleven", "Amazon123").
    - Supports Spanish dates with optional "de" (e.g., "13 de nov de 2025").
    - Parses amounts with currency codes/symbols, parentheses negatives, and trailing minus.
    - Handles decimal comma and thousands separators.

    Args:
        text: Extracted PDF text content
    Returns:
        List of transactions with date, description, and normalized amount string
    """
    transactions: List[Dict[str, str]] = []

    # Currency/amount pattern:
    #  - Optional parentheses for negatives
    #  - Optional currency symbol/code (USD, US$, $, S/, EUR, €, MXN, COP, CLP, ARS, PEN)
    #  - Thousands separators . or , and decimal . or ,
    #  - Optional trailing minus (e.g., 123,45-)
    amount_pattern = (
        r"\(?\s*(?:US\$|USD|\$|S\/|EUR|€|MXN|COP|CLP|ARS|PEN)?\s*"  # currency
        r"-?\d{1,3}(?:[\.,]\d{3})*(?:[\.,]\d{2})?-?\s*\)?"         # number with optional trailing '-'
    )

    # Pattern for numeric dates e.g., 01/15/2024 or 15-01-24
    numeric_date_pattern = rf"(\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})\s+(.+?)\s+({amount_pattern})"

    # Pattern for Spanish month names: 13 nov 2025, 13 noviembre 2025, 13 de nov de 2025
    months = (
        'ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic|'
        'enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre'
    )
    spanish_date_pattern = (
        rf"(\d{{1,2}})\s+(?:de\s+)?({months})\s+(?:de\s+)?(\d{{2,4}})\s+(.+?)\s+({amount_pattern})"
    )

    month_map = {
        'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'ago': 8,
        'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dic': 12,
        'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
        'septiembre': 9, 'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
    }

    def parse_numeric_date(s: str) -> Optional[str]:
        s = s.strip()
        fmts = ['%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y', '%d-%m-%y', '%m/%d/%Y', '%m-%d-%Y', '%m/%d/%y', '%m-%d-%y']
        for fmt in fmts:
            try:
                return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return None

    def normalize_amount(raw: str) -> str:
        # Preserve info to infer negativity
        s = raw.strip()
        negative = False
        if s.startswith('(') and s.endswith(')'):
            negative = True
        # Trailing minus (e.g., 123,45-)
        if re.search(r"-\s*\)?$", s) and not s.strip().startswith('-'):
            negative = True
        # Remove currency words/symbols and parentheses/spaces
        s = re.sub(r"\(|\)|\s", "", s)
        s = re.sub(r"^(?:US\$|USD|\$|S\/|EUR|€|MXN|COP|CLP|ARS|PEN)", "", s, flags=re.IGNORECASE)
        # If both separators present, the rightmost is decimal, the other thousands
        last_dot = s.rfind('.')
        last_comma = s.rfind(',')
        if last_dot != -1 and last_comma != -1:
            # Determine decimal by rightmost
            if last_dot > last_comma:
                # dot decimal, comma thousands
                s = s.replace(',', '')
            else:
                # comma decimal, dot thousands
                s = s.replace('.', '').replace(',', '.')
        elif last_comma != -1 and re.search(r",\d{2}$", s):
            # likely decimal comma
            s = s.replace('.', '').replace(',', '.')
        else:
            # default: remove thousands commas
            s = s.replace(',', '')
        # Normalize sign
        s = s.rstrip('-')
        if negative or raw.strip().startswith('-'):
            if not s.startswith('-'):
                s = '-' + s
        return s

    # Numeric-date matches
    for raw_date, description, amount in re.findall(numeric_date_pattern, text, flags=re.IGNORECASE):
        iso_date = parse_numeric_date(raw_date)
        if not iso_date:
            continue
        transactions.append({
            'date': iso_date,
            'description': description.strip(),
            'amount': normalize_amount(amount)
        })

    # Spanish month matches
    for day, month_name, year, description, amount in re.findall(spanish_date_pattern, text, flags=re.IGNORECASE):
        m = month_map.get(month_name.lower())
        if not m:
            continue
        try:
            y = int(year)
            if y < 100:
                y += 2000 if y < 50 else 1900
            d = int(day)
            dt = datetime(year=y, month=m, day=d)
            iso_date = dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
        transactions.append({
            'date': iso_date,
            'description': description.strip(),
            'amount': normalize_amount(amount)
        })

    # Optional debug: show quick hints when nothing matched
    if DEBUG_PARSE_TRANSACTIONS and not transactions:
        print("[DEBUG] No transactions matched. Preview of extracted text (first 20 lines):")
        lines = text.splitlines()
        for i, line in enumerate(lines[:20], 1):
            print(f"  {i:02d}: {line}")
        print("[DEBUG] Try enabling OCR (set POPPLER_PATH) or adjust patterns.")

    return transactions


def process_invoice(pdf_path: str) -> Dict:
    """
    Process an invoice PDF and extract relevant information.
    
    Args:
        pdf_path: Path to invoice PDF
        
    Returns:
        Dictionary with extracted invoice data
    """
    text = read_pdf_text(pdf_path)
    total = extract_invoice_total(text)
    
    return {
        'file': pdf_path,
        'type': 'invoice',
        'total_amount': total,
        'raw_text': text
    }


def process_bank_statement(pdf_path: str) -> Dict:
    """
    Process a bank statement PDF and extract transactions.
    
    Args:
        pdf_path: Path to bank statement PDF
        
    Returns:
        Dictionary with extracted transaction data
    """
    text = read_pdf_text(pdf_path)
    transactions = extract_bank_transactions(text)
    
    return {
        'file': pdf_path,
        'type': 'bank_statement',
        'transactions': transactions,
        'transaction_count': len(transactions),
        'raw_text': text
    }


def move_with_retry(src: Path, dst: Path, retries: int = 5, delay: float = 0.3) -> None:
    """Move a file with simple retries to avoid Windows file-in-use errors.

    Args:
        src: source path
        dst: destination path
        retries: number of attempts
        delay: base delay in seconds between attempts (exponential backoff)
    """
    for attempt in range(1, retries + 1):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # If destination exists, try to replace it
            if dst.exists():
                try:
                    dst.unlink()
                except PermissionError:
                    # If destination is locked, change the name to avoid collision
                    dst = dst.with_name(f"{dst.stem}_{int(time.time())}{dst.suffix}")
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if attempt == retries:
                raise
            time.sleep(delay * attempt)


def main():
    """
    Example usage of the PDF processing functions.
    """
    # Example: Process an invoice
    # Process all PDF files in the invoice directory
    invoice_dir = Path("invoice")
    account_statement = Path("account_statement")  # Change to your desired directory
    if invoice_dir.exists() and invoice_dir.is_dir():
        pdf_files = list(invoice_dir.glob("*.pdf"))
        if pdf_files:
            for invoice_path in pdf_files:
                invoice_data = process_invoice(str(invoice_path))
                print(f"Processed Invoice: {invoice_data['file']}")
                print(f"Invoice Total: ${invoice_data['total_amount']}")
                # Move processed invoice into a processed_invoices subfolder
                processing_folder = invoice_dir / "processed_invoices"
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                original_name = invoice_path.stem
                extension = invoice_path.suffix
                destination = processing_folder / f"{original_name}_{timestamp}{extension}"
                move_with_retry(invoice_path, destination)
                print(f"Moved invoice to: {destination}\n")
        else:
            print(f"No PDF files found in: {invoice_dir}")
    else:
        print(f"Invoice directory not found: {invoice_dir}")

    if account_statement.exists() and account_statement.is_dir():
        pdf_files = list(account_statement.glob("*.pdf"))
        if pdf_files:
            for pdf_file in pdf_files:
                statement_data = process_bank_statement(str(pdf_file))
                print(f"{pdf_file.name}: Found {statement_data['transaction_count']} transactions")
                for i, transaction in enumerate(statement_data['transactions'][:5], 1):
                    print(f"  {i}. {transaction['date']} - {transaction['description']}: ${transaction['amount']}")
        else:
            print(f"No PDF files found in: {account_statement}")
    else:
        print(f"Statement directory not found: {account_statement}")

    return

if __name__ == "__main__":
    main()