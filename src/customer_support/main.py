"""CLI 入口（click）：ingest / build-kb / ask / serve / tickets。

命令一览：

- ``ingest``    把 ``data/knowledge_base/`` 的文档灌入 RAGFlow（默认检索后端，日常用这个）
- ``build-kb``  构建本地 Milvus 向量库（仅当 ``RETRIEVAL_BACKEND=milvus`` 时使用）
- ``ask``       命令行跑一次问答，验证「意图 → 检索 → 生成 → 转人工」整条链路
- ``serve``     启动 FastAPI 服务
- ``tickets``   查看某天转人工工单汇总（值班复盘用）

设计原则：CLI 只做「参数解析 + 结果打印」，真正的逻辑都在 ``knowledge`` /
``services`` 里，这样同样的能力 API 与脚本都能复用。
"""
from __future__ import annotations

import json
import sys

import click
import uvicorn

from customer_support.common.logging import get_logger, setup_logging
from customer_support.config import get_settings

logger = get_logger(__name__)


@click.group()
def cli() -> None:
    """跨境店铺多语言智能客服 RAG 问答系统。"""
    settings = get_settings()
    setup_logging(settings.log_level, json_format=settings.log_json)
    settings.ensure_dirs()


# ----------------------------------------------------------------------
# 知识库入库（RAGFlow，默认后端）
# ----------------------------------------------------------------------
@cli.command("ingest")
@click.option("--replace", is_flag=True, help="同名文档重新上传（默认跳过，保证幂等）")
@click.option("--prune", is_flag=True, help="删除本地已不存在的远端文档（删除前自动备份）")
@click.option("--no-wait", is_flag=True, help="只触发解析，不等待 deepdoc 跑完")
@click.option("--timeout", default=900.0, show_default=True, help="等待解析完成的上限秒数")
def ingest(replace: bool, prune: bool, no_wait: bool, timeout: float) -> None:
    """把 data/knowledge_base/ 的文档灌入 RAGFlow 知识库。"""
    from customer_support.knowledge.ragflow_ingest import ingest_knowledge_base

    settings = get_settings()
    if not settings.ragflow.api_key:
        raise click.ClickException(
            "未配置 RAGFLOW_API_KEY，无法入库。请在 .env 中填写 RAGFlow 的 API Key。"
        )

    click.echo(f"目标知识库: {settings.ragflow.dataset_ids[0]} @ {settings.ragflow.base_url}")
    click.echo(f"文档目录:   {settings.knowledge.knowledge_base_dir}")

    report = ingest_knowledge_base(
        settings, replace=replace, prune=prune, wait=not no_wait, timeout=timeout
    )

    click.echo("\n========== 入库结果 ==========")
    click.echo(f"知识库 ID:   {report.dataset_id}")
    click.echo(f"新上传:      {len(report.uploaded)} 份")
    click.echo(f"已存在跳过:  {len(report.skipped)} 份")
    click.echo(f"清理远端:    {len(report.deleted)} 份")
    click.echo(f"失败:        {len(report.failed)} 批")
    if report.parse_status:
        done = sum(1 for s in report.parse_status.values() if s == "DONE")
        click.echo(f"解析完成:    {done}/{len(report.parse_status)}")
        for name, status in sorted(report.parse_status.items()):
            if status != "DONE":
                click.echo(f"  ! {name}: {status}")
    for item in report.failed:
        click.echo(f"  x 批次失败 {item.get('batch')}: {item.get('error')}")
    click.echo("==============================\n")

    if not report.ok:
        raise SystemExit(1)


# ----------------------------------------------------------------------
# 本地向量库（Milvus，备选后端）
# ----------------------------------------------------------------------
@cli.command("build-kb")
def build_kb() -> None:
    """构建本地 Milvus 向量库（仅 RETRIEVAL_BACKEND=milvus 时使用）。"""
    from customer_support.common.llm import create_embedding_fn
    from customer_support.knowledge.builder import build_knowledge_base
    from customer_support.rag.milvus_store import MilvusStore

    settings = get_settings()
    if settings.retrieval.backend != "milvus":
        click.echo(
            f"当前 RETRIEVAL_BACKEND={settings.retrieval.backend}，"
            "如需用 Milvus 请先改配置。"
        )
    store = MilvusStore(
        host=settings.milvus.host,
        port=settings.milvus.port,
        collection_name=settings.milvus.collection,
        dim=settings.milvus.embedding_dim,
    )
    n = build_knowledge_base(
        knowledge_dir=settings.knowledge.knowledge_base_dir,
        vector_store=store,
        embedding_fn=create_embedding_fn(settings),
        chunk_size=settings.knowledge.chunk_size,
        chunk_overlap=settings.knowledge.chunk_overlap,
    )
    click.echo(f"知识库构建完成，共 {n} 条。")


# ----------------------------------------------------------------------
# 命令行问答
# ----------------------------------------------------------------------
@cli.command("ask")
@click.argument("question")
@click.option("--language", default=None, help="买家语言（ja/en/es/...，不填则自动识别，默认回退日语）")
@click.option("--session", "session_id", default=None, help="会话 ID（传入则启用多轮上下文）")
@click.option("--json-output", "as_json", is_flag=True, help="以 JSON 输出完整结果")
def ask(question: str, language: str | None, session_id: str | None, as_json: bool) -> None:
    """命令行问答测试。"""
    from customer_support.api.deps import get_chat_service
    from customer_support.models.message import ChatMessage

    service = get_chat_service()
    result = service.handle(
        ChatMessage(question=question, language=language, session_id=session_id)
    )

    if as_json:
        click.echo(result.model_dump_json(indent=2))
        return

    click.echo("\n========== 问答结果 ==========")
    click.echo(f"问题:     {result.question}")
    click.echo(f"语言:     {result.language}")
    click.echo(f"意图:     {result.intent}（置信度 {result.confidence:.2f}）")
    click.echo(f"来源:     {result.source}")
    click.echo(f"转人工:   {result.need_human}" + (f"（{result.human_reason}）" if result.need_human else ""))
    if result.ticket_id:
        click.echo(f"工单号:   {result.ticket_id}")
    click.echo(f"耗时:     {result.latency_ms}ms")
    click.echo(f"回答:     {result.answer}")
    if result.references:
        click.echo("引用:")
        for ref in result.references:
            click.echo(f"  - {ref.source}（相似度 {ref.score:.3f}）")
    click.echo("================================\n")


# ----------------------------------------------------------------------
# API 服务
# ----------------------------------------------------------------------
@cli.command("serve")
@click.option("--host", default="0.0.0.0", help="监听地址")
@click.option("--port", default=8002, help="监听端口")
@click.option("--reload", is_flag=True, help="开发模式：代码变更自动重载")
def serve(host: str, port: int, reload: bool) -> None:
    """启动 FastAPI 服务。"""
    if reload:
        uvicorn.run(
            "customer_support.api.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
        )
    else:
        from customer_support.api.app import create_app

        uvicorn.run(create_app(), host=host, port=port)


# ----------------------------------------------------------------------
# 账号管理（鉴权）
# ----------------------------------------------------------------------
@cli.group("users")
def users_group() -> None:
    """账号管理：创建用户 / 列出用户 / 改密 / 启停。"""


@users_group.command("create")
@click.option("--email", required=True, help="登录邮箱")
@click.option("--password", required=True, help="密码（需满足密码策略）")
@click.option("--name", "full_name", default="", help="显示名")
@click.option("--role", type=click.Choice(["admin", "agent"]), default="agent", show_default=True)
def users_create(email: str, password: str, full_name: str, role: str) -> None:
    """创建账号（跳过自助注册的开放限制，适合开内网号）。"""
    from typing import Literal, cast

    from customer_support.api.deps import get_auth_service

    # click.Choice 已把取值范围限制在 admin/agent，这里只是把类型收敛给 mypy 看
    typed_role = cast("Literal['admin', 'agent']", role)
    user = get_auth_service().create_user(
        email=email, password=password, full_name=full_name, role=typed_role
    )
    click.echo(f"已创建：{user.email}（角色 {user.role}，id {user.id}）")


@users_group.command("list")
def users_list() -> None:
    """列出所有账号。"""
    from customer_support.api.deps import get_auth_service

    rows = get_auth_service().list_all_users()
    if not rows:
        click.echo("暂无账号。")
        return
    click.echo(f"{'邮箱':<34}{'角色':<8}{'状态':<6}最近登录")
    for u in rows:
        last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "从未登录"
        state = "启用" if u.is_active else "停用"
        click.echo(f"{u.email:<34}{u.role:<8}{state:<6}{last}")


@users_group.command("set-password")
@click.option("--email", required=True)
@click.option("--password", required=True)
def users_set_password(email: str, password: str) -> None:
    """管理员重置密码（会撤销该用户所有刷新令牌）。"""
    from customer_support.api.deps import get_auth_service

    get_auth_service().admin_reset_password(email=email, new_password=password)
    click.echo(f"已重置 {email} 的密码，该账号需重新登录。")


@users_group.command("set-active")
@click.option("--email", required=True)
@click.option("--active/--inactive", default=True, help="启用 / 停用账号")
def users_set_active(email: str, active: bool) -> None:
    """启用或停用账号（离职回收）。"""
    from customer_support.api.deps import get_auth_service

    get_auth_service().set_active(email=email, is_active=active)
    click.echo(f"已{'启用' if active else '停用'} {email}")


# ----------------------------------------------------------------------
# 工单复盘
# ----------------------------------------------------------------------
@cli.command("tickets")
@click.option("--day", default=None, help="日期（YYYY-MM-DD），默认今天")
def tickets(day: str | None) -> None:
    """查看转人工工单汇总（按原因 / 意图）。"""
    from datetime import date, datetime, timezone

    from customer_support.api.deps import get_ticket_service

    when = (
        datetime(*date.fromisoformat(day).timetuple()[:3], tzinfo=timezone.utc) if day else None
    )
    summary = get_ticket_service().summary(when)
    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(cli())
