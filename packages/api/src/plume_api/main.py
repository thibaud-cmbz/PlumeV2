from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel


class Health(BaseModel):
    status: Literal["ok"]


app = FastAPI(title="Plume")


@app.get("/health")
def health() -> Health:
    return Health(status="ok")
