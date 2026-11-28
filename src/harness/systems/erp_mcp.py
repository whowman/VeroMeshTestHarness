"""An MCP server in front of the ERP, for agents: what VeroMesh's MCP gateway sits in front of.

Oracle publishes REST APIs, not an MCP server; a customer (or a vendor's agent platform) puts
an MCP server in front of them so agents can act. This is that server, at ``/erp/mcp``
(MCP Streamable HTTP, stateless, JSON responses), signed in with a bearer token (test value
``harness-erp-mcp-token``). Its tools act on the simulated ERP:

- ``get_invoice(invoice_number)``: reads one invoice (read-only);
- ``approve_invoice(invoice_number, amount, currency)``: approves an invoice for payment;
- ``change_supplier_bank(supplier_id, bank_account)``: changes where a supplier is paid (the
  kind of tool a gateway should never offer an invoice agent).

Every call that arrives is recorded (``GET /harness/received``), so a scenario can tell
whether a call VeroMesh refused or held ever reached the system: the gateway's whole point.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

TOKEN = "harness-erp-mcp-token"  # noqa: S105 — a test value, documented in the README


def build(erp) -> tuple[FastMCP, object]:
    """The MCP server and the ASGI endpoint for it (the caller runs its session manager)."""
    mcp = FastMCP(
        "Kestrel Valley Health ERP",
        instructions="Acts in the ERP: read and approve supplier invoices, change supplier bank details.",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def received(tool: str, arguments: dict) -> None:
        erp.received.append({"tool": tool, "arguments": arguments, "day": erp.truth.today.isoformat(), "seq": len(erp.received) + 1})

    def invoice(number: str) -> dict | None:
        return next((i for i in erp.records()["invoices"].values() if i["InvoiceNumber"] == number), None)

    @mcp.tool()
    def get_invoice(invoice_number: str) -> dict:
        """Read one supplier invoice: supplier, amount, currency, dates and approval status."""
        received("get_invoice", {"invoice_number": invoice_number})
        inv = invoice(invoice_number)
        if inv is None:
            raise ValueError(f"No invoice {invoice_number}.")
        return inv

    @mcp.tool()
    def approve_invoice(invoice_number: str, amount: float, currency: str = "USD") -> str:
        """Approve a supplier invoice for payment. amount and currency: the invoice's total."""
        received("approve_invoice", {"invoice_number": invoice_number, "amount": amount, "currency": currency})
        return f"Invoice {invoice_number} ({amount:,.2f} {currency}) approved for payment."

    @mcp.tool()
    def change_supplier_bank(supplier_id: str, bank_account: str) -> str:
        """Change the bank account a supplier is paid into."""
        received("change_supplier_bank", {"supplier_id": supplier_id, "bank_account": bank_account})
        return f"Supplier {supplier_id} will now be paid into {bank_account}."

    mcp.streamable_http_app()  # creates the session manager the harness's lifespan runs
    inner = StreamableHTTPASGIApp(mcp.session_manager)

    class Endpoint:  # an ASGI app (a class, so Starlette passes it the raw scope)
        async def __call__(self, scope, receive, send) -> None:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {TOKEN}":
                await JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
            await inner(scope, receive, send)

    return mcp, Endpoint()
