import random
import time

import click

from boss_agent_cli.api.client import RISK_ERROR_BY_CODE, PlatformRiskError
from boss_agent_cli.api.browser_source import BrowserSourceUnavailable
from boss_agent_cli.api.request_budget import is_request_stop
from boss_agent_cli.api.endpoints import (
	INDUSTRY_CODES,
	JOB_TYPE_CODES,
	SCALE_CODES,
	STAGE_CODES,
)
from boss_agent_cli.api.models import JobItem
from boss_agent_cli.auth.manager import AuthManager, AuthRequired, TokenRefreshFailed
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.compliance import require_compliance_allowed
from boss_agent_cli.commands._platform import get_platform_instance
from boss_agent_cli.display import (
	handle_auth_errors,
	handle_error_output,
	handle_output,
	handle_platform_error_output,
	render_batch_operation_summary,
	render_message_panel,
	risk_error_contract,
)
from boss_agent_cli.search_filters import SearchFilterCriteria, prefilter_platform_job_type
from boss_agent_cli.services.job_identity import deduplicate_job_result


@click.command("greet")
@click.argument("security_id")
@click.argument("job_id")
@click.option("--message", default="", help="自定义打招呼消息")
@click.pass_context
@handle_auth_errors("greet")
def greet_cmd(ctx: click.Context, security_id: str, job_id: str, message: str) -> None:
	"""向指定招聘者打招呼"""
	if not require_compliance_allowed(ctx, "greet"):
		return

	data_dir = ctx.obj["data_dir"]
	logger = ctx.obj["logger"]

	with CacheStore(data_dir / "cache" / "boss_agent.db") as cache:
		if cache.is_greeted(security_id):
			handle_error_output(
				ctx, "greet",
				code="ALREADY_GREETED",
				message="已向该招聘者打过招呼",
				hints={"next_actions": ["boss search <query> — 搜索其他职位"]},
			)
			return
		if cache.is_applied(security_id) is True:
			handle_error_output(
				ctx, "greet",
				code="ALREADY_APPLIED",
				message="已对该招聘者发起立即沟通；已阻止重复打招呼",
			)
			return

		auth = AuthManager(data_dir, logger=logger, platform=ctx.obj.get("platform", "zhipin"))
		with get_platform_instance(ctx, auth) as platform:
			# greet_before hook — allows veto
			hooks = ctx.obj.get("hooks")
			if hooks:
				veto = hooks.greet_before.call({
					"security_id": security_id,
					"job_id": job_id,
					"message": message,
					"source": "greet",
				})
				if veto:
					handle_error_output(
						ctx, "greet", code="HOOK_BLOCKED",
						message=f"打招呼被钩子阻止: {veto}",
						recoverable=True,
					)
					return

			try:
				resp = platform.greet(security_id, job_id, message)
			except Exception as exc:
				if is_request_stop(exc) or isinstance(exc, (AuthRequired, TokenRefreshFailed, BrowserSourceUnavailable)):
					raise
				handle_error_output(
					ctx, "greet", code="ACTION_UNCONFIRMED",
					message="打招呼请求中断，结果未确认",
					recoverable=False,
					recovery_action="先在 BOSS 对应会话核对，勿直接重试",
					details={"outcome": "unknown"},
				)
				return
			if not platform.is_success(resp):
				handle_platform_error_output(
					ctx, "greet", platform, resp,
					fallback_message="打招呼失败",
					unknown_error_code="NETWORK_ERROR",
				)
				return

			cache.record_greet(security_id, job_id)

			# greet_after hook
			if hooks:
				hooks.greet_after.call({
					"security_id": security_id,
					"job_id": job_id,
					"success": True,
					"source": "greet",
				})

			data = {
				"security_id": security_id,
				"job_id": job_id,
				"message": "打招呼成功",
			}
			hints = {
				"next_actions": [
					"boss search <query> — 继续搜索其他职位",
					"boss recommend — 获取个性化推荐",
				],
			}
			handle_output(
				ctx, "greet", data,
				render=lambda d: render_message_panel(d, title="greet"),
				hints=hints,
			)


@click.command("batch-greet")
@click.argument("query")
@click.option("--city", default=None, help="城市名称")
@click.option("--salary", default=None, help="薪资范围")
@click.option("--experience", default=None, help="经验要求（如 3-5年）")
@click.option("--education", default=None, help="学历要求（如 本科）")
@click.option("--industry", default=None, type=click.Choice(list(INDUSTRY_CODES.keys()), case_sensitive=False), help="行业类型")
@click.option("--scale", default=None, type=click.Choice(list(SCALE_CODES.keys()), case_sensitive=False), help="公司规模（如 100-499人）")
@click.option("--stage", default=None, type=click.Choice(list(STAGE_CODES.keys()), case_sensitive=False), help="融资阶段（如 已上市、A轮）")
@click.option("--job-type", default=None, type=click.Choice(list(JOB_TYPE_CODES.keys()), case_sensitive=False), help="职位类型（全职/兼职/实习）")
@click.option("--count", default=10, help="打招呼数量上限（最大 10）")
@click.option("--dry-run", is_flag=True, default=False, help="仅模拟执行，不实际打招呼")
@click.pass_context
@handle_auth_errors("batch-greet")
def batch_greet_cmd(ctx: click.Context, query: str, city: str | None, salary: str | None, experience: str | None, education: str | None, industry: str | None, scale: str | None, stage: str | None, job_type: str | None, count: int, dry_run: bool) -> None:
	"""搜索后批量打招呼（上限 10）"""
	if not require_compliance_allowed(ctx, "batch-greet"):
		return

	data_dir = ctx.obj["data_dir"]
	logger = ctx.obj["logger"]

	count = min(count, 10)

	with CacheStore(data_dir / "cache" / "boss_agent.db") as cache:
		auth = AuthManager(data_dir, logger=logger, platform=ctx.obj.get("platform", "zhipin"))
		with get_platform_instance(ctx, auth) as platform:
			raw = platform.search_jobs(
				query, city=city, salary=salary, experience=experience,
				education=education, industry=industry, scale=scale,
				stage=stage, job_type=job_type,
			)
			if not platform.is_success(raw):
				code, message = platform.parse_error(raw)
				handle_error_output(
					ctx, "batch-greet",
					code=code,
					message=message or "搜索结果获取失败",
					recoverable=False,
				)
				return
			platform_data = platform.unwrap_data(raw) or {}
			job_list = platform_data.get("jobList", [])
			criteria = SearchFilterCriteria(
				query=query,
				city=city,
				salary=salary,
				experience=experience,
				education=education,
				industry=industry,
				scale=scale,
				stage=stage,
				job_type=job_type,
			)

			candidates = []
			seen_recruiters: set[str] = set()
			for raw_item in deduplicate_job_result(job_list):
				matches, _ = prefilter_platform_job_type(raw_item, criteria, platform_name=platform.name)
				if not matches:
					continue
				item = JobItem.from_api(raw_item)
				if not item.security_id or item.security_id in seen_recruiters:
					continue
				seen_recruiters.add(item.security_id)
				if not cache.is_greeted(item.security_id) and cache.is_applied(item.security_id) is not True:
					candidates.append(item)
				if len(candidates) >= count:
					break

			if dry_run:
				items = [item.to_dict() for item in candidates]
				handle_output(
					ctx, "batch-greet", {
						"dry_run": True,
						"candidates": items,
						"count": len(items),
					},
					render=lambda d: render_batch_operation_summary(d, title="batch-greet"),
				)
				return

			results = []
			stopped_reason = None
			risk_error: PlatformRiskError | None = None

			for idx, item in enumerate(candidates):
				success = False
				if cache.is_greeted(item.security_id) or cache.is_applied(item.security_id) is True:
					continue

				try:
					resp = platform.greet(item.security_id, item.job_id)
					if not platform.is_success(resp):
						error_code, error_message = platform.parse_error(resp)
						risk_cls = RISK_ERROR_BY_CODE.get(error_code)
						if risk_cls is not None:
							# 响应字典形态的风控与异常形态走同一条终止分支。
							raise risk_cls(error_message or error_code)
						raise RuntimeError(error_code if error_code != "UNKNOWN" else (resp.get("message") or "greet failed"))
					cache.record_greet(item.security_id, item.job_id)
					results.append({
						"security_id": item.security_id,
						"job_id": item.job_id,
						"title": item.title,
						"company": item.company,
						"status": "success",
					})
					success = True
					logger.info(f"打招呼成功: {item.title} @ {item.company}")
				except PlatformRiskError as exc:
					# 风控：立即停批，不重试、不继续下一个候选人。
					risk_error = exc
					stopped_reason = exc.code
					logger.warning(f"平台风控，停止批量打招呼: {item.title} @ {item.company}")
				except Exception as exc:
					error_msg = str(exc)
					if "RATE_LIMITED" in error_msg or "频率" in error_msg:
						stopped_reason = "RATE_LIMITED"
					elif "GREET_LIMIT" in error_msg or "上限" in error_msg:
						stopped_reason = "GREET_LIMIT"
					else:
						# 发送请求可能已生效；不重试，也不继续后续候选人。
						stopped_reason = "ACTION_UNCONFIRMED"
						results.append({
							"security_id": item.security_id,
							"job_id": item.job_id,
							"title": item.title,
							"company": item.company,
							"status": "unconfirmed",
							"error_type": type(exc).__name__,
						})
						logger.warning(f"打招呼结果未确认，已停止批次: {item.title} @ {item.company}")

				if stopped_reason:
					break

				if success and idx < len(candidates) - 1:
					bg_delay = ctx.obj.get("config", {}).get("batch_greet_delay", [2.0, 5.0])
					time.sleep(random.uniform(bg_delay[0], bg_delay[1]))

			data = {
				"greeted": [r for r in results if r["status"] == "success"],
				"failed": [r for r in results if r["status"] == "failed"],
				"unconfirmed": [r for r in results if r["status"] == "unconfirmed"],
				"total_greeted": sum(1 for r in results if r["status"] == "success"),
				"total_failed": sum(1 for r in results if r["status"] == "failed"),
			}
			if stopped_reason:
				data["stopped_reason"] = stopped_reason

			if risk_error is not None:
				# 停批原因是风控时信封必须是 ok:false + 对应错误码 + recoverable=false，
				# 而不是 ok:true + stopped_reason——后者会让 Agent 当成「等一会儿重试」。
				# 已成功的部分随 details 带回，避免 Agent 对同一批候选人重复打招呼。
				recovery_action, hints = risk_error_contract(risk_error.code)
				handle_error_output(
					ctx, "batch-greet",
					code=risk_error.code,
					message=str(risk_error) or risk_error.code,
					recoverable=False,
					recovery_action=recovery_action,
					details=data,
					hints=hints or None,
				)
				return

			if stopped_reason == "ACTION_UNCONFIRMED":
				handle_error_output(
					ctx, "batch-greet",
					code="ACTION_UNCONFIRMED",
					message="打招呼结果未确认，已停止后续发送",
					recoverable=False,
					recovery_action="在 BOSS 消息页核对目标会话，勿直接重发",
					details=data,
				)
				return

			handle_output(
				ctx, "batch-greet", data,
				render=lambda d: render_batch_operation_summary(d, title="batch-greet"),
			)
