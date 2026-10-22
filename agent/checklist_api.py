"""로컬 MVP의 체크리스트 API. AI 진단 상태와 수동 작업 상태는 분리한다."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class CreateChecklist(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=200)


class UpdateItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['pending', 'in_progress', 'done', 'deferred']
    note: str = Field(default='', max_length=2000)


@router.get('/checklists')
def list_checklists(request: Request):
    return {'checklists': request.app.state.checklists.list()}


@router.post('/checklists')
def create_checklist(body: CreateChecklist, request: Request):
    try:
        return request.app.state.checklists.create(body.run_id, body.thread_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.patch('/checklists/{checklist_id}/items/{item_id}')
def update_item(checklist_id: str, item_id: str, body: UpdateItem, request: Request):
    try:
        return request.app.state.checklists.update(checklist_id, item_id, body.status, body.note)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
