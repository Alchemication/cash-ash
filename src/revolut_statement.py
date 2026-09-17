"""Extract English Revolut account PDFs as evidence, never ledger entries.

Native currency amounts remain in the source document model. They are not EUR
accounting amounts and must not be inserted into trades, cash flows or prices.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from config import REVOLUT_MONEY_TOLERANCE

_DATE = r"\d{2} [A-Z][a-z]{2} \d{4}"
_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY = rf"(?:US\$|€|£|USD\s*|EUR\s*|GBP\s*){_NUMBER}"
_CURRENCY = {"USD": ("US$", "USD"), "EUR": ("€", "EUR"), "GBP": ("£", "GBP")}


@dataclass(frozen=True)
class StatementHolding:
    """One reported holding; values and prices belong to its currency section."""

    symbol: str
    company: str
    isin: str
    quantity: Decimal
    price: Decimal
    value: Decimal


@dataclass(frozen=True)
class StatementTransaction:
    """A broker activity row, preserved for review without importing a fill."""

    timestamp: str
    details: str
    value: Decimal
    fees: Decimal
    commission: Decimal


@dataclass
class CurrencySection:
    """Separate native balances and portfolio breakdown for one currency."""

    currency: str
    starting: dict[str, Decimal] = field(default_factory=dict)
    ending: dict[str, Decimal] = field(default_factory=dict)
    breakdown: dict[str, Decimal] = field(default_factory=dict)
    holdings: list[StatementHolding] = field(default_factory=list)
    transactions: list[StatementTransaction] = field(default_factory=list)


@dataclass
class Statement:
    """Document dates are distinct; no valuation timestamp is asserted by the PDF."""

    period_start: date
    period_end: date
    generated_on: date
    sections: list[CurrencySection]
    valuation_at: None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return serializable evidence, keeping decimals lossless as strings."""
        import json

        return json.loads(json.dumps(asdict(self), default=str))


def _day(value: str) -> date:
    """Parse the English PDF date independently of the machine's locale."""
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    try:
        day, month, year = value.split()
        return date(int(year), months.index(month) + 1, int(day))
    except ValueError as exc:
        raise ValueError(
            "Invalid statement date; export an English account PDF and inspect its dates."
        ) from exc


def _money(value: str, currency: str) -> Decimal:
    """Validate the currency marker before parsing a native amount."""
    for prefix in _CURRENCY.get(currency, ()):
        if value.startswith(prefix):
            number = value[len(prefix) :].strip()
            if re.fullmatch(_NUMBER, number):
                return Decimal(number.replace(",", ""))
    raise ValueError("Unsupported amount or currency; inspect the original PDF.")


def extract_pdf(path: Path | BytesIO) -> Statement:
    """Extract a text-based English account statement using pypdf layout mode."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF; export an unencrypted account statement.")
        pages = [
            page.extract_text(extraction_mode="layout") if "/Contents" in page else ""
            for page in reader.pages
        ]
    except PdfReadError as exc:
        raise ValueError(
            "Unreadable PDF; download the account statement again."
        ) from exc
    return parse_layout("\n".join(pages))


def parse_layout(text: str) -> Statement:
    """Parse the verified account layout, refusing incomplete or unknown tables."""
    generated = set(re.findall(rf"Generated on the ({_DATE})", text))
    periods = set(re.findall(rf"Period\s+({_DATE})\s+-\s+({_DATE})", text))
    if "Account Statement" not in text or len(generated) != 1 or len(periods) != 1:
        raise ValueError(
            "Missing or inconsistent statement dates; use an English account PDF."
        )
    start, end = next(iter(periods))
    statement = Statement(_day(start), _day(end), _day(generated.pop()), [])
    if statement.period_start > min(statement.period_end, statement.generated_on):
        raise ValueError("Invalid statement period; inspect the original PDF.")
    section: CurrencySection | None = None
    mode = ""
    seen_tables: set[tuple[str, str]] = set()
    for original in text.splitlines():
        line = original.strip()
        if not line:
            continue
        heading = re.fullmatch(
            r"([A-Z]{3}) (Account summary|Portfolio breakdown|Transactions)", line
        )
        if heading:
            currency, mode = heading.groups()
            if currency not in _CURRENCY:
                raise ValueError(
                    f"Unsupported currency {currency}; inspect the PDF manually."
                )
            if (currency, mode) in seen_tables:
                raise ValueError("Repeated statement table; inspect the PDF manually.")
            seen_tables.add((currency, mode))
            if mode == "Account summary":
                section = CurrencySection(currency)
                statement.sections.append(section)
            elif section is None or section.currency != currency:
                raise ValueError(
                    "Statement tables out of order; inspect the original PDF."
                )
            continue
        if line == "Glossary":
            mode = ""
        if section is None or not mode:
            continue
        if line == "Account Statement" or line.startswith("Generated on the "):
            continue
        if line.startswith(("Symbol ", "Date ", "Starting ", "*Cash value")):
            continue
        if mode in {"Account summary", "Portfolio breakdown"}:
            balance = re.fullmatch(
                rf"(Positions Value|Cash value\*?|Total)\s+({_MONEY})(?:\s+({_MONEY}))?(?:\s+\d+(?:\.\d+)?%)?",
                line,
            )
            if balance:
                label, first, second = balance.groups()
                key = {
                    "Positions Value": "positions",
                    "Cash value": "cash",
                    "Total": "total",
                }[label.rstrip("*")]
                target = (
                    section.ending if mode == "Account summary" else section.breakdown
                )
                if key in target or (mode == "Account summary" and second is None):
                    raise ValueError(
                        "Duplicate or incomplete balance row; inspect the PDF."
                    )
                target[key] = _money(second or first, section.currency)
                if mode == "Account summary":
                    section.starting[key] = _money(first, section.currency)
                continue
        if mode == "Portfolio breakdown":
            holding = re.fullmatch(
                rf"(\S+)\s+(.+?)\s+([A-Z]{{2}}[A-Z0-9]{{9}}\d)\s+({_NUMBER})\s+({_MONEY})\s+({_MONEY})\s+\d+(?:\.\d+)?%",
                line,
            )
            if not holding:
                raise ValueError(
                    "Unrecognised portfolio row; inspect the PDF rather than accepting missing holdings."
                )
            symbol, company, isin, quantity, price, value = holding.groups()
            row = StatementHolding(
                symbol,
                company,
                isin,
                Decimal(quantity.replace(",", "")),
                _money(price, section.currency),
                _money(value, section.currency),
            )
            if min(row.quantity, row.price, row.value) < 0:
                raise ValueError("Negative holding is unsupported; inspect the PDF.")
            if any(h.symbol == symbol or h.isin == isin for h in section.holdings):
                raise ValueError("Duplicate holding; inspect the PDF.")
            section.holdings.append(row)
        elif mode == "Transactions":
            transaction = re.fullmatch(
                rf"({_DATE} \d{{2}}:\d{{2}}:\d{{2}} [A-Z]+)\s+(.+?)\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})",
                line,
            )
            if not transaction:
                raise ValueError(
                    "Unrecognised transaction row; inspect the PDF manually."
                )
            timestamp, details, value, fees, commission = transaction.groups()
            _day(timestamp[:11])
            datetime.strptime(timestamp[12:20], "%H:%M:%S")
            section.transactions.append(
                StatementTransaction(
                    timestamp,
                    re.sub(r"\s+", " ", details),
                    _money(value, section.currency),
                    _money(fees, section.currency),
                    _money(commission, section.currency),
                )
            )
        elif mode == "Account summary":
            raise ValueError("Unrecognised summary row; inspect the PDF manually.")
    _validate(statement, seen_tables)
    statement.warnings.append(
        "Valuation timestamp is not stated. Generation date is not a market-price timestamp."
    )
    if statement.generated_on < statement.period_end:
        statement.warnings.append(
            "Generated before period end: this is a partial-period statement."
        )
    statement.warnings.append(
        "Transactions are parsed here as evidence; writing them to the ledger is a "
        "separate step (revolut_fills.import_activity)."
    )
    return statement


def _validate(statement: Statement, seen_tables: set[tuple[str, str]]) -> None:
    """Reject omitted holdings and inconsistent native-currency balances."""
    tolerance = Decimal(REVOLUT_MONEY_TOLERANCE)
    if not statement.sections:
        raise ValueError(
            "No currency sections; export a text-based English account PDF."
        )
    for section in statement.sections:
        for mode in ("Account summary", "Portfolio breakdown", "Transactions"):
            if (section.currency, mode) not in seen_tables:
                raise ValueError("Incomplete statement; download all PDF pages.")
        for balances in (section.starting, section.ending, section.breakdown):
            if set(balances) != {"positions", "cash", "total"}:
                raise ValueError("Missing statement balances; download all PDF pages.")
            if (
                abs(balances["positions"] + balances["cash"] - balances["total"])
                > tolerance
            ):
                raise ValueError("Statement balances do not add up; inspect the PDF.")
        if any(
            abs(section.ending[key] - section.breakdown[key]) > tolerance
            for key in section.ending
        ):
            raise ValueError(
                "Summary and portfolio breakdown disagree; inspect the PDF."
            )
        total = sum((h.value for h in section.holdings), Decimal(0))
        if abs(total - section.ending["positions"]) > tolerance * max(
            1, len(section.holdings)
        ):
            raise ValueError(
                "Holdings do not reconcile to the section total; inspect the PDF."
            )
