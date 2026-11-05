"""Terminal formatting and currency display helpers for Eluno POS CLI."""

from typing import Any, List, Optional, Sequence


def format_inr(paise: int) -> str:
    """Format integer paise into standard Indian Rupee string with 2 decimals.

    Examples:
        1949700 -> '₹19,497.00'
        0 -> '₹0.00'
        -50000 -> '-₹500.00'
    """
    if paise is None:
        return "₹0.00"

    is_negative = paise < 0
    abs_paise = abs(paise)
    rupees = abs_paise // 100
    cents = abs_paise % 100

    # Format rupees with Indian comma grouping: last 3 digits, then groups of 2
    s = str(rupees)
    if len(s) <= 3:
        formatted_rupees = s
    else:
        last3 = s[-3:]
        remaining = s[:-3]
        parts = []
        while len(remaining) > 2:
            parts.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            parts.insert(0, remaining)
        formatted_rupees = ",".join(parts) + "," + last3

    res = f"₹{formatted_rupees}.{cents:02d}"
    return f"-{res}" if is_negative else res


def print_header(title: str, subtitle: Optional[str] = None) -> None:
    """Print a visually distinct CLI section header."""
    width = 72
    print("\n" + "=" * width)
    print(f"  {title.upper()}")
    if subtitle:
        print(f"  {subtitle}")
    print("=" * width)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Print an aligned ASCII table."""
    str_rows = [[str(cell) for cell in row] for row in rows]
    col_widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))
            else:
                col_widths.append(len(cell))

    # Format header
    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(col_widths)))

    print(header_line)
    print(sep_line)
    for row in str_rows:
        padded_row = [row[i].ljust(col_widths[i]) if i < len(row) else "".ljust(col_widths[i]) for i in range(len(col_widths))]
        print(" | ".join(padded_row))


def print_card(title: str, items: Sequence[tuple[str, Any]]) -> None:
    """Print a key-value summary card."""
    width = 68
    print("\n┌" + "─" * (width - 2) + "┐")
    print(f"│  {title.ljust(width - 5)}│")
    print("├" + "─" * (width - 2) + "┤")
    for key, val in items:
        line_content = f"  {key}: {val}"
        if len(line_content) > width - 4:
            line_content = line_content[: width - 7] + "..."
        print(f"│{line_content.ljust(width - 2)}│")
    print("└" + "─" * (width - 2) + "┘")


def prompt_input(prompt_text: str, default: Optional[str] = None) -> str:
    """Prompt the user for input with an optional default value."""
    disp = f"{prompt_text} [{default}]: " if default else f"{prompt_text}: "
    try:
        val = input(disp).strip()
        return val if val else (default or "")
    except (EOFError, OSError):
        return default or ""


def prompt_confirm(prompt_text: str, default: bool = False) -> bool:
    """Prompt user for a yes/no confirmation."""
    def_str = "Y/n" if default else "y/N"
    try:
        resp = input(f"{prompt_text} [{def_str}]: ").strip().lower()
        if not resp:
            return default
        return resp in ("y", "yes")
    except (EOFError, OSError):
        return default
