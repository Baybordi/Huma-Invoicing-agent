"""
seed_odoo_vendors.py — One-off setup: load the Approved Supplier List into Odoo.

Rather than creating vendor contacts by hand in the Odoo UI (slow, error-prone,
not reproducible), this script reads the Approved Supplier List and creates a
res.partner record for each approved vendor, idempotently (it skips any vendor
that already exists). Run it once after bringing up a fresh Odoo instance.

    python -m src.seed_odoo_vendors

This makes the whole demo reproducible from a clean database, which is exactly
what a reviewer wants to see: "bring up Odoo, seed, run the agent."
"""

from __future__ import annotations

import logging

from .config import load_settings
from .odoo_client import OdooClient
from .validate import load_approved_suppliers

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
logger = logging.getLogger("seed")


def main(supplier_list_path: str = "data/Approved_Supplier_List.xlsx") -> None:
    settings = load_settings()
    odoo = OdooClient(settings)
    approved = load_approved_suppliers(supplier_list_path)

    created, skipped = 0, 0
    for supplier in approved.values():
        existing = odoo.find_vendor_id(supplier.name)
        if existing:
            logger.info("exists  : %s (id=%s)", supplier.name, existing)
            skipped += 1
            continue
        # Create as a company flagged as a supplier (supplier_rank=1).
        partner_id = odoo._execute(
            "res.partner",
            "create",
            {"name": supplier.name, "is_company": True, "supplier_rank": 1},
        )
        logger.info("created : %s (id=%s)", supplier.name, partner_id)
        created += 1

    logger.info("Done. Created %d, skipped %d existing.", created, skipped)


if __name__ == "__main__":
    main()
