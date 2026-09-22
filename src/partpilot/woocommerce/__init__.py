"""Read-only WooCommerce REST client (product catalog).

Used to pull the webshop's product list (keyed by SKU) into PartPilot's catalog. We only
ever read from Woo — see ``client.WooClient``.
"""

from .client import WooClient, WooError, WooProduct

__all__ = ["WooClient", "WooError", "WooProduct"]
