"""Portfolio proposals with deterministic validation and atomic publication."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from config import DECISION_MAX_TOKENS
from llm import call_llm
from models import Security
from profiles import Profile, read_context
from guardrails import GuardrailContext, Verdict
from research import extract_json, load_prompt, triage_inputs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Portfolio decision
# ---------------------------------------------------------------------------

DECIDE_PROMPT_VERSION = "decide/4"

_VALID_ACTIONS = {"BUY", "ADD", "HOLD", "TRIM", "EXIT", "REVIEW", "KEEP_CASH"}
_VALID_URGENCY = {"low", "medium", "high"}


def build_guardrail_context(
    conn: sqlite3.Connection,
    *,
    profile: Profile | None = None,
    account_id: int = 1,
    today: date | None = None,
) -> GuardrailContext:
    """Assemble the portfolio state the deterministic checks run against."""
    from config import DEFAULT_MONTHLY_CONTRIBUTION_EUR
    from guardrails import GuardrailContext
    from portfolio import cash_eur, holdings, total_value
    from store_research import active_theses

    rows = holdings(conn, account_id=account_id)
    cash = cash_eur(conn, account_id=account_id)
    total = total_value(rows, cash=cash)
    theses = active_theses(conn)

    weights: dict[str, float] = {}
    values: dict[str, float] = {}
    statuses: dict[str, str] = {}
    for row in rows:
        security = row.position.security
        assert security.id is not None
        if row.value_eur is not None and total:
            weights[security.ticker] = row.value_eur / total * 100
            values[security.ticker] = row.value_eur
        thesis = theses.get(security.id)
        statuses[security.ticker] = thesis.thesis_status if thesis else "unexamined"

    from quality import valuation_gaps
    from workflow import capital_committed

    now = today or date.today()
    reserved, weekly, committed = capital_committed(conn, now)
    gaps = valuation_gaps(conn, rows, now)
    for ticker, amount in committed.items():
        values[ticker] = values.get(ticker, 0) + amount
        weights[ticker] = values[ticker] / total * 100 if total else 0
    pending_tickers = frozenset(
        row["ticker"]
        for row in conn.execute("""
        SELECT s.ticker FROM recommendation r JOIN securities s ON s.id=r.security_id
        WHERE r.action IN ('BUY','ADD','TRIM','EXIT')
        AND (SELECT decision FROM user_decision d WHERE d.recommendation_id=r.id ORDER BY d.id DESC LIMIT 1)='approve'
        AND NOT EXISTS(SELECT 1 FROM execution e WHERE e.recommendation_id=r.id)
    """)
    )
    return GuardrailContext(
        pending_tickers=pending_tickers,
        reserved_cash_eur=reserved,
        weekly_committed_eur=weekly,
        blocked_reason="Trade blocked: " + "; ".join(gaps) if gaps else None,
        total_value_eur=total,
        cash_eur=cash,
        monthly_contribution_eur=(
            profile.monthly_contribution_eur
            if profile is not None
            else DEFAULT_MONTHLY_CONTRIBUTION_EUR
        ),
        weights_by_ticker=weights,
        values_by_ticker=values,
        thesis_status_by_ticker=statuses,
    )


def run_decision(
    conn: sqlite3.Connection,
    *,
    profile: Profile | None = None,
    account_id: int = 1,
    today: date | None = None,
    blocked_reason: str | None = None,
) -> tuple[int, list[dict], str]:
    """Propose recommendations, then enforce the deterministic rules on them.

    The model proposes; :mod:`guardrails` decides. A proposal that breaches a
    position cap, a trade limit or the owner's own sell discipline is refused
    or reduced here rather than argued with in a prompt, which is the reason a
    model is allowed near this decision at all.

    Every surviving recommendation records the price and FX rate at the time.
    That is the forward-tracking the evaluation rests on and it cannot be
    reconstructed later.

    Args:
        conn: Open database connection.
        profile: Profile supplying the contribution figure.
        account_id: Account to decide for.
        today: Reference date, for tests.

    Returns:
        ``(research run id, stored recommendations, summary)``.

    Raises:
        ValueError: If there is nothing to decide on, or output is unusable.
    """
    from config import RECOMMENDATION_EXPIRY_DAYS
    from guardrails import check_proposal
    from store import latest_prices, load_securities
    from store_research import create_research_run

    inputs = triage_inputs(
        conn, account_id=account_id, today=today, include_sell_eligibility=True
    )
    if not inputs:
        raise ValueError("No holdings to decide on.")

    context = build_guardrail_context(
        conn, profile=profile, account_id=account_id, today=today
    )
    if blocked_reason:
        context = replace(context, blocked_reason=blocked_reason)
    run_date = (today or date.today()).isoformat()
    run_id = create_research_run(
        conn, run_date=run_date, kind="deep", note="Portfolio decision"
    )

    securities = load_securities(conn)
    prices = latest_prices(conn)
    owner_context = (
        read_context(profile, names=("strategy.md", "investor.md")) if profile else {}
    )
    message = "\n\n".join(
        [
            f"Portfolio decision for {run_date}.",
            f"Total EUR {context.total_value_eur:,.2f} · cash EUR "
            f"{context.cash_eur:,.2f} · planned monthly contribution EUR "
            f"{context.monthly_contribution_eur:,.2f}.",
            f"Reserved cash EUR {context.reserved_cash_eur:,.2f} · available funded cash EUR "
            f"{context.available_capital_eur:,.2f} · committed this week EUR "
            f"{context.weekly_committed_eur:,.2f}.",
            "Pending execution: " + ", ".join(sorted(context.pending_tickers)),
            "Owner context (null means unwritten or no profile):\n"
            + json.dumps(
                {
                    name: owner_context.get(name)
                    for name in ("strategy.md", "investor.md")
                }
            ),
            "\n\n".join(entry.render() for entry in inputs),
            _market_context(conn, securities, prices),
            _research_context(conn),
            f"Trade availability: {context.blocked_reason or 'subject to funded cash and portfolio constraints'}.",
        ]
    )

    result = call_llm(
        conn,
        feature="decision",
        messages=[
            {"role": "system", "content": load_prompt("decide.md")},
            {"role": "user", "content": message},
        ],
        prompt_version=DECIDE_PROMPT_VERSION,
        max_tokens=DECISION_MAX_TOKENS,
    )
    payload = extract_json(result.text)

    expires = (
        (today or date.today()) + timedelta(days=RECOMMENDATION_EXPIRY_DAYS)
    ).isoformat()

    proposals = _validate_proposals(payload, securities)

    conn.execute("SAVEPOINT publish_decision")
    try:
        conn.execute(
            """UPDATE recommendation SET superseded_by_run_id=?
            WHERE superseded_by_run_id IS NULL AND COALESCE(
                (SELECT decision FROM user_decision d WHERE d.recommendation_id=recommendation.id
                 ORDER BY d.id DESC LIMIT 1), '') NOT IN ('approve','reject')""",
            (run_id,),
        )
        context = build_guardrail_context(
            conn, profile=profile, account_id=account_id, today=today
        )
        if blocked_reason:
            context = replace(context, blocked_reason=blocked_reason)
        stored: list[dict] = []
        allocated = 0.0
        simulated_values = dict(context.values_by_ticker)

        for raw in proposals:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action", "")).strip().upper()
            if action not in _VALID_ACTIONS:
                logger.warning("Discarding proposal with unknown action %r", action)
                continue
            # HOLD carries no instruction and no consequence; storing one per
            # untouched holding would bury the few rows that mean something.
            if action == "HOLD":
                continue

            ticker = raw.get("ticker")
            ticker = str(ticker).strip().upper() if ticker else None
            if ticker and ticker not in securities:
                logger.warning("Discarding proposal for unknown ticker %r", ticker)
                continue

            amount = raw.get("amount_eur")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None

            verdict = check_proposal(
                action=action,
                ticker=ticker,
                amount_eur=amount,
                context=replace(
                    context,
                    allocated_this_run_eur=allocated,
                    values_by_ticker=simulated_values,
                    weights_by_ticker={
                        t: v / context.total_value_eur * 100
                        for t, v in simulated_values.items()
                    }
                    if context.total_value_eur
                    else {},
                ),
            )
            if not verdict.refused and action == "BUY":
                from quality import security_price_gap

                gap = security_price_gap(
                    conn, securities[ticker].id, today or date.today()
                )
                if gap:
                    verdict = Verdict(action, None, refused=True, refusal=gap)
            if not verdict.refused and action in {"BUY", "ADD", "EXIT"}:
                from store_workflow import latest_assessments
                from config import RESEARCH_OVERDUE_DAYS

                assessment = next(
                    (a for a in latest_assessments(conn) if a["ticker"] == ticker), None
                )
                age = (
                    (
                        (today or date.today())
                        - date.fromisoformat(assessment["run_date"])
                    ).days
                    if assessment
                    else None
                )
                if (
                    assessment is None
                    or assessment["coverage"] != "sufficient"
                    or not 0 <= age < RESEARCH_OVERDUE_DAYS
                ):
                    verdict = Verdict(
                        action,
                        None,
                        refused=True,
                        refusal="Fresh research with verified citations is required; review the evidence first.",
                    )
            if verdict.refused:
                conn.execute(
                    "INSERT INTO decision_refusal(run_id,ticker,action,rationale,refusal) VALUES (?,?,?,?,?)",
                    (run_id, ticker, action, raw["rationale"], verdict.refusal),
                )
                logger.info(
                    "Guardrail refused %s %s: %s", action, ticker, verdict.refusal
                )
                stored.append(
                    {
                        "id": None,
                        "ticker": ticker,
                        "action": action,
                        "amount_eur": None,
                        "refused": True,
                        "refusal": verdict.refusal,
                        "rationale": str(raw.get("rationale", "")).strip(),
                        "urgency": "low",
                        "adjustments": (),
                    }
                )
                continue

            if verdict.amount_eur and verdict.action in {"BUY", "ADD"}:
                allocated += verdict.amount_eur
                simulated_values[ticker] = (
                    simulated_values.get(ticker, 0) + verdict.amount_eur
                )

            urgency = str(raw.get("urgency", "low")).strip().lower()
            security = securities.get(ticker) if ticker else None
            price_row = prices.get(security.id) if security and security.id else None

            recommendation_id = _store_recommendation(
                conn,
                run_date=run_date,
                run_id=run_id,
                security=security,
                action=verdict.action,
                amount_eur=verdict.amount_eur,
                rationale=str(raw.get("rationale", "")).strip() or "no rationale given",
                urgency=urgency if urgency in _VALID_URGENCY else "low",
                price_row=price_row,
                weight_pct=context.weights_by_ticker.get(ticker) if ticker else None,
                expires_on=expires,
                verdict=verdict,
                llm_call_id=result.llm_call_id,
            )
            stored.append(
                {
                    "id": recommendation_id,
                    "ticker": ticker,
                    "action": verdict.action,
                    "amount_eur": verdict.amount_eur,
                    "refused": False,
                    "refusal": None,
                    "rationale": str(raw.get("rationale", "")).strip(),
                    "urgency": urgency if urgency in _VALID_URGENCY else "low",
                    "adjustments": verdict.adjustments,
                }
            )
        conn.execute(
            "INSERT INTO decision_batch(run_id,summary,created_at) VALUES (?,?,?)",
            (run_id, str(payload.get("summary", "")), datetime.now(UTC).isoformat()),
        )
        conn.execute("RELEASE publish_decision")
    except Exception:
        conn.execute("ROLLBACK TO publish_decision")
        conn.execute("RELEASE publish_decision")
        raise

    return run_id, stored, str(payload.get("summary", "")).strip()


def _store_recommendation(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    run_id: int,
    security: Security | None,
    action: str,
    amount_eur: float | None,
    rationale: str,
    urgency: str,
    price_row: sqlite3.Row | None,
    weight_pct: float | None,
    expires_on: str,
    verdict: Verdict,
    llm_call_id: int | None,
) -> int:
    """Persist one recommendation with the price that stood behind it."""
    from datetime import UTC, datetime

    from store_research import active_thesis

    fx = None
    value_eur = None
    price_native = price_row["close_native"] if price_row is not None else None
    if price_row is not None:
        row = conn.execute(
            """
            SELECT rate FROM fx_rates WHERE base = ? AND quote = 'EUR'
            ORDER BY rate_date DESC LIMIT 1
            """,
            (price_row["currency"],),
        ).fetchone()
        fx = (
            float(row["rate"])
            if row
            else (1.0 if price_row["currency"] == "EUR" else None)
        )
        if fx is not None and price_native is not None:
            value_eur = price_native * fx

    thesis = (
        active_thesis(conn, security_id=security.id)
        if security is not None and security.id is not None
        else None
    )

    cursor = conn.execute(
        """
        INSERT INTO recommendation (
            run_date, research_run_id, security_id, action, amount_eur,
            rationale, urgency, thesis_id, price_native, fx_rate, value_eur,
            weight_pct, expires_on, guardrails, adjusted, llm_call_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_date,
            run_id,
            security.id if security is not None else None,
            action,
            amount_eur,
            rationale,
            urgency,
            thesis.id if thesis else None,
            price_native,
            fx,
            value_eur,
            weight_pct,
            expires_on,
            json.dumps(
                {
                    "checks": list(verdict.checks),
                    "adjustments": list(verdict.adjustments),
                }
            ),
            int(verdict.adjusted),
            llm_call_id,
            datetime.now(UTC).isoformat(),
        ),
    )

    return int(cursor.lastrowid)


def _market_context(
    conn: sqlite3.Connection,
    securities: dict[str, Security],
    prices: dict[int, sqlite3.Row],
) -> str:
    """Render stored native closes and dated FX without inventing valuations."""
    quotes: dict[str, dict] = {}
    for ticker, security in securities.items():
        price = prices.get(security.id)
        currency = price["currency"] if price is not None else security.currency
        fx = conn.execute(
            "SELECT rate, rate_date, source FROM fx_rates WHERE base=? AND quote='EUR' "
            "ORDER BY rate_date DESC LIMIT 1",
            (currency,),
        ).fetchone()
        quotes[ticker] = {
            "pricing_mode": security.pricing_mode,
            "latest_stored_close": {
                "price_native": price["close_native"],
                "currency": price["currency"],
                "date": price["price_date"],
                "source": price["source"],
            }
            if price is not None
            else None,
            "fx_base": currency,
            "fx_quote": "EUR",
            "fx_to_eur": {"rate": 1.0, "rate_date": None, "source": "EUR identity"}
            if currency == "EUR"
            else dict(fx)
            if fx is not None
            else None,
        }
    return "Stored market data (null means unavailable):\n" + json.dumps(quotes)


def _research_context(conn: sqlite3.Connection) -> str:
    """Render assessments separately from owner-approved theses, with dated evidence."""
    from store_workflow import latest_assessments

    parts = ["Research assessments (not automatically adopted by the owner):"]
    for row in latest_assessments(conn):
        proposal = conn.execute(
            "SELECT status FROM thesis WHERE research_run_id=? ORDER BY id DESC LIMIT 1",
            (row["run_id"],),
        ).fetchone()
        owner_review = proposal["status"] if proposal else "no revision proposed"
        parts.append(f"Owner review of this assessment: {owner_review}.")
        parts.append(
            f"{row['ticker']} — {row['run_date']}, coverage {row['coverage']}, "
            f"status {row['status']}: {row['reason']}\n"
            f"Findings: {row['answers_json']}\nOpen questions: {row['open_questions_json']}"
        )
    if len(parts) == 1:
        parts.append(
            "No completed research available. Do not infer that theses were checked."
        )
    return "\n".join(parts)


def _validate_proposals(payload: dict, securities: dict) -> list[dict]:
    """Reject malformed batches before retiring any previous recommendations."""
    proposals = payload.get("recommendations")
    if not isinstance(proposals, list):
        raise ValueError(
            "Decision output needs a recommendations list. Retry main.py recommend."
        )
    seen: set[str | None] = set()
    for raw in proposals:
        if not isinstance(raw, dict) or raw.get("action") not in _VALID_ACTIONS:
            raise ValueError("Invalid recommendation action. Retry main.py recommend.")
        ticker = raw.get("ticker")
        if ticker is not None and (
            not isinstance(ticker, str) or ticker.upper() not in securities
        ):
            raise ValueError("Unknown recommendation ticker. Retry main.py recommend.")
        ticker = ticker.upper() if ticker else None
        if ticker in seen:
            raise ValueError(
                "Duplicate or conflicting recommendations. Retry main.py recommend."
            )
        seen.add(ticker)
        if raw["action"] in {"BUY", "ADD", "TRIM", "EXIT"} and ticker is None:
            raise ValueError("Trade recommendation needs a ticker.")
        if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
            raise ValueError("Recommendation needs an explanation.")
        amount = raw.get("amount_eur")
        if amount is not None and (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise ValueError("Recommendation amount must be finite and positive.")
        if raw["action"] in {"BUY", "ADD", "TRIM"} and amount is None:
            raise ValueError("Sized trade recommendation needs an amount.")
        if raw["action"] in {"HOLD", "REVIEW", "KEEP_CASH"} and amount is not None:
            raise ValueError("Non-trade recommendation must not carry an amount.")
    return proposals
