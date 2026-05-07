"""FinPay Support — the deliberately fragile target agent Mender watches.

Hosted as its own Cloud Run service (or local FastAPI app) so Mender can call
it for eval runs and so the traffic generator has somewhere to send queries.
"""

__version__ = "0.1.0"
