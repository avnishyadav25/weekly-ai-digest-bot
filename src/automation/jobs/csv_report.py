"""csv-report: group the rows of a CSV file (a path or a URL) by one column, sum others, email an HTML table."""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import Field

from automation.jobs.base import Context, Job, JobConfig, JobFailed, Result
from automation.notify import Message


class CsvReportConfig(JobConfig):
    source: str = Field(description="CSV file path, or an http(s) URL")
    group_by: str = Field(description="column to group rows by")
    sum: list[str] = Field(default_factory=list, description="numeric columns to total per group")
    title: str = "CSV report"
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    once_per: Literal["day", "week", "month", "run"] = Field(
        default="week", description="send at most one report per period; 'run' sends every time"
    )


def period_key(now: datetime, once_per: str) -> str | None:
    if once_per == "day":
        return now.strftime("%Y-%m-%d")
    if once_per == "week":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"
    if once_per == "month":
        return now.strftime("%Y-%m")
    return None


def _number(raw: str, column: str, line: int) -> Decimal:
    value = raw.strip().replace(",", "") if raw else ""
    if not value:
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation:
        raise JobFailed(f"line {line}: {column} = {raw!r} is not a number") from None


def _fmt(value: Decimal, decimals: bool) -> str:
    return f"{value:,.2f}" if decimals else f"{value:,.0f}"


class CsvReport(Job[CsvReportConfig]):
    name = "csv-report"
    description = "Aggregates a CSV export into an HTML table and emails it."
    schedule = "0 8 * * 1"
    config_model = CsvReportConfig

    def read_source(self, ctx: Context[CsvReportConfig]) -> str:
        source = ctx.config.source
        if source.startswith(("http://", "https://")):
            return ctx.http.get(source).text
        path = Path(source)
        if not path.is_file():
            raise JobFailed(f"CSV file not found: {path}")
        return path.read_text(encoding="utf-8-sig")

    def aggregate(self, text: str, cfg: CsvReportConfig) -> tuple[dict[str, dict[str, Decimal]], int]:
        reader = csv.DictReader(io.StringIO(text), delimiter=cfg.delimiter)
        header = reader.fieldnames or []
        missing = [c for c in [cfg.group_by, *cfg.sum] if c not in header]
        if missing:
            raise JobFailed(f"column(s) {', '.join(missing)} not in the CSV header ({', '.join(header)})")
        groups: dict[str, dict[str, Decimal]] = {}
        count = 0
        for count, row in enumerate(reader, start=1):
            key = (row.get(cfg.group_by) or "").strip() or "(blank)"
            group = groups.setdefault(key, {"rows": Decimal(0), **{c: Decimal(0) for c in cfg.sum}})
            group["rows"] += 1
            for column in cfg.sum:
                group[column] += _number(row.get(column) or "", column, count + 1)
        return groups, count

    def run(self, ctx: Context[CsvReportConfig]) -> Result:
        cfg = ctx.config
        period = period_key(ctx.now, cfg.once_per)
        if period and ctx.state.seen(f"report:{period}"):
            return Result(summary=f"report for {period} already sent; skipped", data={"sent": False})

        groups, rows = self.aggregate(self.read_source(ctx), cfg)
        sort_column = cfg.sum[0] if cfg.sum else "rows"
        ordered = sorted(groups.items(), key=lambda item: item[1][sort_column], reverse=True)
        columns = ["rows", *cfg.sum]
        totals = {c: sum((g[c] for g in groups.values()), Decimal(0)) for c in columns}

        # A column is shown with two decimals when any of its values has a fractional part.
        decimals = {c: any(g[c] != g[c].to_integral_value() for g in groups.values()) for c in columns}
        header = [cfg.group_by, *columns]
        table = [[name, *(_fmt(values[c], decimals[c]) for c in columns)] for name, values in ordered]
        table.append(["Total", *(_fmt(totals[c], decimals[c]) for c in columns)])
        widths = [max(len(str(r[i])) for r in [header, *table]) for i in range(len(header))]
        text_lines = [
            "  ".join(str(cell).ljust(w) for cell, w in zip(r, widths, strict=True)) for r in [header, *table]
        ]
        text = f"{rows} rows from {cfg.source}" + (f" (period {period})" if period else "") + "\n\n"
        text += "\n".join(line.rstrip() for line in text_lines)

        def row_html(cells: list[str], tag: str = "td", bold: bool = False) -> str:
            out = []
            for i, value in enumerate(cells):
                align = "left" if i == 0 else "right"
                content = f"<b>{html.escape(value)}</b>" if bold else html.escape(value)
                out.append(
                    f'<{tag} style="padding:4px 10px;border-bottom:1px solid #ddd;text-align:{align}">'
                    f"{content}</{tag}>"
                )
            return "<tr>" + "".join(out) + "</tr>"

        body_rows = [row_html(r) for r in table[:-1]] + [row_html(table[-1], bold=True)]
        intro = f"{rows} rows from {html.escape(cfg.source)}" + (f" (period {period})" if period else "")
        report_html = (
            f"<p>{intro}</p>"
            '<table style="border-collapse:collapse;font-family:sans-serif;font-size:14px">'
            f"<thead>{row_html(header, 'th')}</thead><tbody>{''.join(body_rows)}</tbody></table>"
        )
        title = f"{cfg.title}" + (f" ({period})" if period else "")
        ctx.notify.send(Message(title=title, text=text, html=report_html))
        if period:
            ctx.state.mark_seen([f"report:{period}"])
        return Result(
            summary=f"report sent: {rows} rows in {len(groups)} groups", data={"rows": rows, "sent": True}
        )
