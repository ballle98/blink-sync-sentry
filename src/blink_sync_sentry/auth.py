"""Blink authentication helpers: credential loading, 2FA, token persistence."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

from aiohttp import ClientSession
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink

from blink_sync_sentry.config import AccountConfig

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication cannot proceed."""


async def _load_token(path: Path) -> dict | None:
    """Load a saved token/credential file (JSON)."""
    if not path.is_file():
        _LOGGER.debug("No saved token file at %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _LOGGER.info("Loaded saved credentials from %s", path)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        _LOGGER.warning("Could not load token file %s: %s", path, exc)
        return None


async def _save_token(blink: Blink, path: Path) -> None:
    """Persist current auth tokens to disk for future sessions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    await blink.save(str(path))
    
    # Set secure permissions (owner read/write only)
    try:
        current_permissions = path.stat().st_mode
        path.chmod(current_permissions & ~stat.S_IRWXG & ~stat.S_IRWXO | stat.S_IRUSR | stat.S_IWUSR)
        _LOGGER.info("Saved credentials to %s with secure permissions", path)
    except OSError as exc:
        _LOGGER.warning("Could not set secure permissions on %s: %s", path, exc)


async def create_blink(acct: AccountConfig) -> Blink:
    """Create, authenticate, and return a ready-to-use Blink instance.

    Attempts to load a saved token first.  Falls back to username/password.
    Handles 2FA interactively unless *acct.no_prompt* is set.

    :param acct: Account configuration with credentials and token path.
    :raises AuthError: If authentication fails or 2FA is required in
        non-interactive mode without a valid token.
    """
    session = ClientSession()
    blink = Blink(session=session)

    try:
        token_path = Path(acct.token_file)
        saved = await _load_token(token_path)

        if saved:
            auth = Auth(saved, no_prompt=acct.no_prompt)
        elif acct.username and acct.password:
            auth = Auth(
                {"username": acct.username, "password": acct.password},
                no_prompt=acct.no_prompt,
            )
        else:
            await session.close()
            raise AuthError(
                f"Account '{acct.name}': no saved token and no credentials provided. "
                f"Set BLINK_USERNAME / BLINK_PASSWORD (or BLINK_{acct.name.upper()}_USERNAME "
                f"/ BLINK_{acct.name.upper()}_PASSWORD for multi-account), "
                "or run an interactive login first to create a token file."
            )

        blink.auth = auth

        try:
            await blink.start()
        except Exception as exc:
            exc_name = type(exc).__name__
            if "TwoFA" in exc_name or "2fa" in str(exc).lower():
                if acct.no_prompt:
                    await session.close()
                    raise AuthError(
                        f"Account '{acct.name}': 2FA is required but --no-prompt is set. "
                        "Run an interactive session first:\n"
                        "  blink-sync-sentry list   (without --no-prompt)\n"
                        "Then re-run with --no-prompt or in watch mode."
                    ) from exc
                _LOGGER.info(
                    "Account '%s': 2FA required — check your email for the PIN.",
                    acct.name,
                )
                pin = _prompt_2fa(acct.name)
                await auth.complete_2fa_login(pin)
                await blink.start()  # Restart after 2FA to setup account_id and other attributes
            else:
                await session.close()
                raise AuthError(
                    f"Account '{acct.name}': Blink authentication failed: {exc}"
                ) from exc

        await _save_token(blink, token_path)
        return blink

    except Exception:
        # Ensure session is closed on any error
        if not session.closed:
            await session.close()
        raise


def _prompt_2fa(account_name: str = "default") -> str:
    """Prompt the user for a 2FA PIN on stdin."""
    try:
        pin = input(f"Enter 2FA PIN for account '{account_name}': ").strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        raise AuthError("2FA PIN entry cancelled.") from None
    if not pin:
        raise AuthError("Empty 2FA PIN.")
    return pin


async def close_blink(blink: Blink) -> None:
    """Cleanly close the Blink session."""
    session = getattr(blink.auth, "session", None)
    if session and not session.closed:
        await session.close()
