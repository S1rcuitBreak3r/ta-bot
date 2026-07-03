"""
Module flag helpers.

Three states: 'disabled', 'admin_only', 'all_users'.
Admin always passes regardless of module state.
"""
import core.db as db
from core.config import ADMIN_TELEGRAM_ID

ALL_MODULES = ("onboarding", "knowledge_bank", "expiry_tracker", "education", "form_filler")


def is_module_enabled(module_name: str, telegram_id: int) -> bool:
    """
    Returns True if the user is allowed to use this module.
    Admin always passes. Non-admin passes only if state is 'all_users'.
    """
    if telegram_id == ADMIN_TELEGRAM_ID or db.is_admin(telegram_id):
        return True
    state = db.get_module_state(module_name)
    return state == "all_users"


def module_maintenance_message() -> str:
    return "This feature is temporarily offline for maintenance. Please try again shortly."


def enable_module(module_name: str, target: str = "all_users"):
    """target must be 'admin_only' or 'all_users'."""
    assert target in ("admin_only", "all_users"), f"Invalid target: {target}"
    db.set_module_state(module_name, target)


def disable_module(module_name: str):
    db.set_module_state(module_name, "disabled")
