"""Money manipulation utilities using integer paise and Decimal half-up rounding.

Rule: Float types are strictly prohibited for financial accounting in Eluno POS.
All internal monetary calculations are performed in integer paise (₹1.00 = 100 paise).
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Union


def parse_to_paise(amount: Union[str, int, float, Decimal]) -> int:
    """Convert a rupee amount (string, int, or Decimal) into integer paise.

    Examples:
        parse_to_paise("10998.00") -> 1099800
        parse_to_paise(10998) -> 1099800
        parse_to_paise("5400") -> 540000
    """
    if isinstance(amount, int):
        # Assume whole rupees if passed as plain int (e.g. 500 -> 50000 paise)
        # unless it already represents a large paise value; to be unambiguous:
        return amount * 100

    dec_val = Decimal(str(amount).strip())
    # Multiply by 100 and quantize to nearest integer
    paise_dec = (dec_val * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise_dec)


def format_paise_to_rupees(paise: int) -> str:
    """Format integer paise into standard 2-decimal rupee string (e.g. 1099800 -> '10998.00')."""
    sign = "-" if paise < 0 else ""
    abs_paise = abs(paise)
    rupees = abs_paise // 100
    cents = abs_paise % 100
    return f"{sign}{rupees}.{cents:02d}"


def round_half_up(value: Union[Decimal, float, str]) -> int:
    """Perform exact Half-Up rounding on a decimal/fractional value to the nearest integer.

    Examples:
        round_half_up(Decimal("7.5")) -> 8
        round_half_up(Decimal("7.4")) -> 7
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split_paise_evenly(total_paise: int, count: int) -> List[int]:
    """Split an integer paise amount into 'count' parts, distributing odd remainder paise to earlier parts.

    Guarantees: sum(result) == total_paise.
    """
    if count <= 0:
        return []
    base_part = total_paise // count
    remainder = total_paise % count

    parts = []
    for i in range(count):
        part = base_part + (1 if i < remainder else 0)
        parts.append(part)
    return parts


def allocate_discount_proportionally(
    total_discount_paise: int,
    line_prices_paise: List[int],
) -> List[int]:
    """Allocate a discount across line items proportionally based on their prices.

    Guarantees:
    1. sum(allocations) == total_discount_paise (unless total_discount > total_price, in which case it is capped).
    2. No allocation exceeds the corresponding line price (non-negative line prices).
    3. Remainder paise from rounding are assigned to the largest eligible line item.
    """
    total_price = sum(line_prices_paise)
    if total_price <= 0 or total_discount_paise <= 0:
        return [0] * len(line_prices_paise)

    # Cap discount at total price so lines never turn negative
    effective_discount = min(total_discount_paise, total_price)

    # Initial proportional distribution using ROUND_HALF_UP
    allocations: List[int] = []
    allocated_sum = 0

    for price in line_prices_paise:
        if price <= 0:
            allocations.append(0)
            continue
        share = Decimal(effective_discount * price) / Decimal(total_price)
        alloc = round_half_up(share)
        # Cap at price
        alloc = min(alloc, price)
        allocations.append(alloc)
        allocated_sum += alloc

    # Reconcile rounding discrepancy to exact paisa
    diff = effective_discount - allocated_sum
    if diff != 0:
        # Find indices of lines that have room for adjustment
        if diff > 0:
            # Need to add paise: sort by line price descending
            indices = sorted(range(len(line_prices_paise)), key=lambda i: line_prices_paise[i], reverse=True)
            for idx in indices:
                room = line_prices_paise[idx] - allocations[idx]
                add_amt = min(diff, room)
                allocations[idx] += add_amt
                diff -= add_amt
                if diff == 0:
                    break
        else:
            # Need to subtract paise (allocated too much): sort by allocation descending
            indices = sorted(range(len(allocations)), key=lambda i: allocations[i], reverse=True)
            for idx in indices:
                sub_amt = min(-diff, allocations[idx])
                allocations[idx] -= sub_amt
                diff += sub_amt
                if diff == 0:
                    break

    return allocations

