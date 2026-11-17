from fastapi import APIRouter, Body, Request

from . import service

router = APIRouter(tags=["M7-模拟支付与撤回对账"])


@router.get("/payments")
def page(request: Request):
    data = service.overview(request)
    return request.app.state.templates.TemplateResponse(
        request, "m7_payments.html", {"title": "支付对账", "data": data})


@router.post("/api/payments/pools")
def create_pool(request: Request, payload: dict = Body(...)):
    return service.create_pool(request, payload)


@router.get("/api/payments/pools")
def list_pools(request: Request):
    return service.list_pools(request)


@router.post("/api/payments/batches")
def create_batch(request: Request, payload: dict = Body(...)):
    return service.create_batch(request, payload)


@router.get("/api/payments/batches")
def list_batches(request: Request, pool_id: str | None = None):
    return service.list_batches(request, pool_id)


@router.get("/api/payments/batches/{batch_id}")
def batch_detail(request: Request, batch_id: str):
    return service.batch_detail(request, batch_id)


@router.get("/api/payments/positions")
def positions(request: Request, pool_id: str | None = None):
    return service.positions(request, pool_id)


@router.post("/api/payments/instructions/{instruction_id}/send")
def send_payment(request: Request, instruction_id: str):
    return service.send_payment(request, instruction_id)


@router.get("/api/payments/instructions/{instruction_id}/bank-status")
def bank_status(request: Request, instruction_id: str):
    return service.bank_status(request, instruction_id)


@router.post("/api/payments/instructions/{instruction_id}/receipt")
def post_receipt(request: Request, instruction_id: str, payload: dict = Body(...)):
    return service.post_receipt(request, instruction_id, str(payload["outcome"]))


@router.post("/api/payments/withdraw")
def withdraw(request: Request, payload: dict = Body(...)):
    return service.withdraw(request, payload)


@router.post("/api/payments/instructions/{instruction_id}/recover")
def register_recovery(request: Request, instruction_id: str, payload: dict = Body(...)):
    return service.register_recovery(request, instruction_id, str(payload["amount"]))


@router.get("/api/payments/events")
def events(request: Request, ref_id: str | None = None):
    return service.list_events(request, ref_id)


@router.post("/api/payments/scenarios")
def set_scenario(request: Request, payload: dict = Body(...)):
    return service.set_scenario(request, payload)
