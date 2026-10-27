import click

from boss_agent_cli.api.browser_source import BrowserSourceUnavailable
from boss_agent_cli.api.request_budget import is_request_stop
from boss_agent_cli.auth.manager import AuthManager, AuthRequired, TokenRefreshFailed
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.compliance import require_compliance_allowed
from boss_agent_cli.commands._platform import get_platform_instance
from boss_agent_cli.display import boss_command_for_ctx, handle_auth_errors, handle_error_output, handle_output, handle_platform_error_output, render_message_panel


@click.command("apply")
@click.argument("security_id")
@click.argument("job_id")
@click.option("--lid", default="", help="列表项 ID（可选）")
@click.pass_context
@handle_auth_errors("apply")
def apply_cmd(ctx: click.Context, security_id: str, job_id: str, lid: str) -> None:
	"""发起立即沟通；本命令不会上传或发送附件简历。"""
	if not require_compliance_allowed(ctx, "apply"):
		return

	data_dir = ctx.obj["data_dir"]
	logger = ctx.obj["logger"]

	with CacheStore(data_dir / "cache" / "boss_agent.db") as cache:
		if cache.is_applied(security_id):
			handle_error_output(
				ctx,
				"apply",
				code="ALREADY_APPLIED",
				message="已对该招聘者发起过立即沟通；附件简历状态需另行核对",
				hints={
					"next_actions": [
						boss_command_for_ctx(ctx, "me --section deliver"),
						boss_command_for_ctx(ctx, "chat"),
					]
				},
			)
			return
		if cache.is_greeted(security_id) is True:
			handle_error_output(
				ctx, "apply",
				code="ALREADY_GREETED",
				message="已向该招聘者打过招呼；立即沟通共用同一发送接口，已阻止重复联系",
			)
			return

		auth = AuthManager(data_dir, logger=logger, platform=ctx.obj.get("platform", "zhipin"))
		with get_platform_instance(ctx, auth) as platform:
			try:
				resp = platform.apply(security_id, job_id, lid=lid)
			except Exception as exc:
				if is_request_stop(exc) or isinstance(exc, (AuthRequired, TokenRefreshFailed, BrowserSourceUnavailable)):
					raise
				handle_error_output(
					ctx, "apply", code="ACTION_UNCONFIRMED",
					message="立即沟通请求中断，结果未确认",
					recoverable=False,
					recovery_action="先在 BOSS 对应会话核对，勿直接重试",
					details={"outcome": "unknown"},
				)
				return
			if not platform.is_success(resp):
				handle_platform_error_output(
					ctx,
					"apply",
					platform,
					resp,
					fallback_message="立即沟通提交失败",
					unknown_error_code="NETWORK_ERROR",
				)
				return
			cache.record_apply(security_id, job_id)

	handle_output(
		ctx,
		"apply",
		{
			"security_id": security_id,
			"job_id": job_id,
			"lid": lid,
			"mode": "immediate_chat_apply",
			"chat_request_submitted": True,
			"message_delivery_confirmed": False,
			"attachment_resume_sent": False,
			"message": "立即沟通请求已提交；消息送达与附件简历发送需分别核对",
		},
		render=lambda d: render_message_panel(d, title="apply"),
		hints={
			"next_actions": [
				boss_command_for_ctx(ctx, "me --section deliver"),
				boss_command_for_ctx(ctx, "chat"),
			]
		},
	)
