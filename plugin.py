"""
上下文清除插件

提供命令来清除聊天的消息历史记录，让麦麦"忘记"之前的对话
"""

from typing import List, Tuple, Optional
from datetime import datetime, timedelta

from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    register_plugin,
    ConfigField,
)
from src.common.logger import get_logger
from src.common.database.database_model import Messages

logger = get_logger("context_clear")


class ClearContextCommand(BaseCommand):
    """清除上下文命令"""

    command_name = "clear_context"
    command_description = "清除聊天上下文命令"
    command_pattern = r"(?P<clear_cmd>^/(clear|清除上下文|清空上下文).*$)"

    async def execute(self) -> Tuple[bool, str, bool]:
        """执行命令"""
        # 权限检查
        if (
            not self.message
            or not self.message.message_info
            or not self.message.message_info.user_info
            or str(self.message.message_info.user_info.user_id) not in self.get_config("plugin.permission", [])
        ):
            await self.send_text("❌ 你没有权限使用清除上下文命令")
            return False, "没有权限", True

        command_text = self.matched_groups.get("clear_cmd", "").strip()
        parts = command_text.split()

        # 获取当前聊天ID
        chat_id = None
        if self.message and hasattr(self.message, 'chat_stream') and self.message.chat_stream:
            chat_id = self.message.chat_stream.stream_id

        if not chat_id:
            await self.send_text("❌ 无法获取聊天ID")
            return True, "无法获取聊天ID", True

        # 解析参数
        if len(parts) == 1:
            # 只有命令，显示帮助
            await self._show_help()
            return True, "显示帮助", True

        subcommand = parts[1] if len(parts) > 1 else ""

        if subcommand == "all":
            # 清除所有消息
            await self._clear_all(chat_id)
        elif subcommand == "recent":
            # 清除最近N条消息
            count = int(parts[2]) if len(parts) > 2 else 10
            await self._clear_recent(chat_id, count)
        elif subcommand == "before":
            # 清除指定时间之前的消息
            hours = int(parts[2]) if len(parts) > 2 else 24
            await self._clear_before_hours(chat_id, hours)
        elif subcommand == "help":
            await self._show_help()
        else:
            await self.send_text(f"未知命令: {subcommand}\n使用 /clear help 查看帮助")

        return True, "命令执行完成", True

    async def _clear_all(self, chat_id: str):
        """清除所有消息"""
        try:
            # 获取消息总数
            count = Messages.select().where(Messages.chat_id == chat_id).count()

            if count == 0:
                await self.send_text("当前聊天没有消息记录")
                return

            # 删除所有消息
            deleted = Messages.delete().where(Messages.chat_id == chat_id).execute()

            await self.send_text(f"✅ 已清除所有上下文\n\n删除了 {deleted} 条消息记录")
            logger.info(f"清除了聊天 {chat_id} 的所有消息，共 {deleted} 条")

        except Exception as e:
            logger.error(f"清除所有消息失败: {e}", exc_info=True)
            await self.send_text(f"❌ 清除失败: {str(e)}")

    async def _clear_recent(self, chat_id: str, count: int):
        """清除最近N条消息"""
        try:
            # 获取最近的N条消息
            recent_messages = (Messages
                              .select()
                              .where(Messages.chat_id == chat_id)
                              .order_by(Messages.time.desc())
                              .limit(count))

            if not recent_messages:
                await self.send_text("没有找到消息记录")
                return

            # 获取这些消息的ID
            message_ids = [msg.message_id for msg in recent_messages]

            # 删除这些消息
            deleted = Messages.delete().where(Messages.message_id.in_(message_ids)).execute()

            await self.send_text(f"✅ 已清除最近 {count} 条消息\n\n实际删除了 {deleted} 条记录")
            logger.info(f"清除了聊天 {chat_id} 的最近 {deleted} 条消息")

        except Exception as e:
            logger.error(f"清除最近消息失败: {e}", exc_info=True)
            await self.send_text(f"❌ 清除失败: {str(e)}")

    async def _clear_before_hours(self, chat_id: str, hours: int):
        """清除指定小时之前的消息"""
        try:
            # 计算时间戳
            threshold_time = (datetime.now() - timedelta(hours=hours)).timestamp()

            # 删除指定时间之前的消息
            deleted = (Messages
                      .delete()
                      .where(
                          (Messages.chat_id == chat_id) &
                          (Messages.time < threshold_time)
                      )
                      .execute())

            if deleted == 0:
                await self.send_text(f"没有找到 {hours} 小时之前的消息")
            else:
                await self.send_text(f"✅ 已清除 {hours} 小时前的消息\n\n删除了 {deleted} 条记录")
                logger.info(f"清除了聊天 {chat_id} 的 {hours} 小时前消息，共 {deleted} 条")

        except Exception as e:
            logger.error(f"清除历史消息失败: {e}", exc_info=True)
            await self.send_text(f"❌ 清除失败: {str(e)}")

    async def _show_help(self):
        """显示帮助"""
        help_text = """🗑️ 上下文清除命令

⚠️  此命令需要管理员权限

用法:
/clear all - 清除所有消息记录
/clear recent [数量] - 清除最近N条消息（默认10条）
/clear before [小时] - 清除N小时前的消息（默认24小时）
/clear help - 显示此帮助

示例:
/clear all              # 清除所有上下文
/clear recent 20        # 清除最近20条消息
/clear before 48        # 清除48小时前的消息

⚠️ 警告: 清除后无法恢复！

权限配置:
管理员可在 plugins/context_clear_plugin/config.toml 中配置
permission = ["用户ID1", "用户ID2"]"""

        await self.send_text(help_text)


@register_plugin
class ContextClearPlugin(BasePlugin):
    """上下文清除插件"""

    plugin_name: str = "context_clear_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用上下文清除插件"
            ),
            "config_version": ConfigField(
                type=str,
                default="1.0.0",
                description="配置文件版本"
            ),
            "permission": ConfigField(
                type=list,
                default=["1334431750"],
                description="有权限使用清除上下文命令的用户ID列表"
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple]:
        """获取插件组件"""
        return [
            (ClearContextCommand.get_command_info(), ClearContextCommand),
        ]
