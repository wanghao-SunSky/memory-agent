"""OTel：recall 和每个作业各一条 trace，导出到 Jaeger（OTLP/HTTP）。默认不记录正文。"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from . import config

_provider: TracerProvider | None = None


def setup_tracing(service_name: str, exporter: SpanExporter | None = None, simple: bool = False) -> TracerProvider:
    """exporter 为 None 时按配置导出到 OTLP；测试里可传 InMemorySpanExporter。"""
    global _provider
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is None and config.otlp_endpoint():
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint())
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter) if simple else BatchSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # 已设置过（测试中多次调用）
        pass
    _provider = provider
    # Pydantic AI 的 GenAI span：include_content=False => 不记录提示词和输出正文
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=False, include_binary_content=False,
                                                include_model_request_parameters=False))
    return provider


def tracer() -> trace.Tracer:
    if _provider is not None:
        return _provider.get_tracer("memory_agent")
    return trace.get_tracer("memory_agent")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def force_flush() -> None:
    if _provider is not None:
        _provider.force_flush()
