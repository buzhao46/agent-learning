"""Day 3 · FastAPI 入门：路由 / Pydantic 模型 / 依赖注入

对应你 Java 里熟悉的东西：
    @RestController + @GetMapping  -> @app.get(...)
    @RequestBody + DTO             -> Pydantic BaseModel
    @Autowired                     -> Depends(get_xxx)
    Validation 注解                 -> Field(...) 约束
    自动 Swagger                   -> http://127.0.0.1:8000/docs

运行：
    pip install fastapi uvicorn
    python day3_fastapi.py
    浏览器打开 http://127.0.0.1:8000/docs

或用命令行验收：
    curl http://127.0.0.1:8000/health
    curl -X POST http://127.0.0.1:8000/echo -H "Content-Type: application/json" -d "{\"message\":\"hello\"}"
    curl http://127.0.0.1:8000/devices/core-sw-01
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Day 3 Demo", version="0.1.0")

# ---------------------------------------------------------------- 数据层
DEVICES: dict[str, dict[str, Any]] = {
    "core-sw-01": {"name": "core-sw-01", "ip": "10.0.0.1", "status": "up", "cpu": 92},
    "core-rtr-02": {"name": "core-rtr-02", "ip": "10.0.0.2", "status": "up", "cpu": 41},
    "agg-sw-03": {"name": "agg-sw-03", "ip": "10.0.0.3", "status": "down", "cpu": -1},
}


# ---------------------------------------------------------------- 模型
class EchoIn(BaseModel):
    """请求体模型：字段类型/长度都在这声明，非法请求 FastAPI 自动返回 422。"""

    message: str = Field(..., min_length=1, max_length=200, description="要回显的文本")
    upper: bool = Field(False, description="是否转大写")


class EchoOut(BaseModel):
    echo: str
    length: int


class DeviceOut(BaseModel):
    name: str
    ip: str
    status: str
    cpu: int


# ---------------------------------------------------------------- 依赖注入
class Settings(BaseModel):
    """生产项目里这里换成读环境变量 / 配置中心。"""

    app_name: str = "snmp-agent"
    cpu_warn: int = 80
    api_token: str = ""  # 空 = 不校验，方便本地调试


@lru_cache  # 只构造一次，等价 Spring 的单例 Bean
def get_settings() -> Settings:
    return Settings()


# 依赖的「现代写法」：把 Depends 放进类型里，而不是参数默认值。
# 好处：mypy/ruff 都能正确推断类型，也不会触发「函数调用写在默认参数里」的告警。
SettingsDep = Annotated[Settings, Depends(get_settings)]
TokenDep = Annotated[str | None, Header()]


def require_token(x_token: TokenDep = None, settings: SettingsDep = None) -> None:  # type: ignore[assignment]
    """依赖里也能再依赖别的依赖。设置 api_token 后所有挂它的接口都要带 X-Token。"""
    assert settings is not None
    if settings.api_token and x_token != settings.api_token:
        raise HTTPException(status_code=401, detail="X-Token 无效")


# ---------------------------------------------------------------- 路由
@app.get("/health")
def health(settings: SettingsDep) -> dict[str, object]:
    return {"status": "ok", "app": settings.app_name, "devices": len(DEVICES)}


@app.post("/echo", response_model=EchoOut)
def echo(body: EchoIn) -> EchoOut:
    text = body.message.upper() if body.upper else body.message
    return EchoOut(echo=text, length=len(text))


@app.get("/devices", dependencies=[Depends(require_token)])
def list_devices(settings: SettingsDep) -> dict[str, object]:
    abnormal = [d for d in DEVICES.values() if d["status"] != "up" or int(d["cpu"]) >= settings.cpu_warn]
    return {"total": len(DEVICES), "abnormal": abnormal}


@app.get("/devices/{name}", response_model=DeviceOut)
def get_device(name: str) -> DeviceOut:
    """路径参数 + 404 处理：查不到就抛 HTTPException，别返回 None。"""
    dev = DEVICES.get(name)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"device not found: {name}")
    return DeviceOut(**dev)  # type: ignore[arg-type]


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
