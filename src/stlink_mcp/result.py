"""How every tool says whether it worked.

The project has one rule: a tool reports success only after checking that the
thing it was asked to do actually happened. That rule used to be kept by hand,
separately, in each tool. It drifted, in every direction it could:

- one tool returned a literal `ok: True` whether or not it had done anything,
- one computed `ok` from whether any console bytes arrived, without checking
  that the load those bytes were supposed to prove had actually completed,
- one returned `ok: True` out of a cache while its docstring said it had just
  probed the hardware,
- one reported `verified_by: "nothing"` alongside `ok: True`, which is the rule
  written out and then not applied.

Each of those was individually small and locally reasonable. Together they meant
the guarantee the project advertises was not a property of the code, only a
habit. So `ok` is no longer something a tool decides. It is a consequence of
which constructor was called, and every constructor carries its evidence.

Three outcomes:

    verified(evidence, ...)    ok=True    checked, and this is what was observed
    unverified(reason, ...)    ok=False   may well have worked, nothing checked
    failed(error, ...)         ok=False   it did not work, and this is why

`unverified` is the interesting one, and the reason `ok` is False there. An
operation nobody watched is not a success, however likely it is to have worked.
Reporting it as one is the exact harm this project exists to prevent, so the
conservative answer is the default and the caller gets `unverified_because` to
tell it apart from a real failure.
"""

from __future__ import annotations

from typing import Any

VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"

#: Keys the constructors own. A caller passing these would be trying to set the
#: verdict by hand, which is the thing this module exists to stop.
_RESERVED = ("ok", "status", "verified_by", "unverified_because", "error")


class EvidenceError(RuntimeError):
    """A result was constructed without the evidence its outcome requires."""


def _check(fields: dict[str, Any]) -> None:
    clash = [key for key in _RESERVED if key in fields]
    if clash:
        raise EvidenceError(
            f"{', '.join(clash)} is set by the constructor, not by the caller. "
            "Choose the constructor that matches what actually happened."
        )


def verified(evidence: str, **fields: Any) -> dict[str, Any]:
    """Report success, naming the observation that established it.

    `evidence` is not decoration. It is what a reader consults when deciding how
    far to trust `ok`, so it names what was observed ("console output from this
    board's own port", "words read back from the target") rather than what was
    attempted ("ran the load command"). If the honest evidence string would be
    "the vendor tool said so", write that, because it tells the reader exactly
    how much the success is worth.
    """
    if not evidence or not evidence.strip():
        raise EvidenceError(
            "verified() needs the evidence that established success. If nothing "
            "observed the effect, the honest constructor is unverified()."
        )
    _check(fields)
    return {**fields, "ok": True, "status": VERIFIED, "verified_by": evidence.strip()}


def unverified(reason: str, **fields: Any) -> dict[str, Any]:
    """The operation may well have worked. Nothing established that it did.

    Deliberately `ok: False`. The alternative, a true `ok` carrying a caveat in
    a neighbouring field, was tried and is what this module replaced: callers
    gate on `ok` and do not read the caveat, including this project's own dev
    harness.
    """
    if not reason or not reason.strip():
        raise EvidenceError(
            "unverified() needs the reason nothing could be checked, since that "
            "is the only thing separating it from a plain failure."
        )
    _check(fields)
    return {
        **fields,
        "ok": False,
        "status": UNVERIFIED,
        "verified_by": None,
        "unverified_because": reason.strip(),
    }


def failed(error: str, **fields: Any) -> dict[str, Any]:
    """It did not work, and this is why.

    Reserved for a known negative: something was observed and it was wrong, or
    the operation could not be attempted at all. When the truth is "nobody
    looked", that is `unverified`, not this.
    """
    if not error or not error.strip():
        raise EvidenceError("failed() needs the reason it failed.")
    _check(fields)
    return {**fields, "ok": False, "status": FAILED, "verified_by": None,
            "error": error.strip()}
