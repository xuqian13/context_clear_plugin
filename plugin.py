"""
麦麦失忆插件

让麦麦选择性"失忆"，忘记之前的对话~
"""

from typing import List, Tuple, Optional, Dict, ClassVar
from datetime import datetime, timedelta
import os
import json
import shutil
import time
import asyncio

from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    register_plugin,
    ConfigField,
)
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import EventType, MaiMessages
from src.common.logger import get_logger
from src.common.database.database_model import (
    Messages,
    ChatStreams,
    PersonInfo,
    Expression,
    ActionRecords,
    ChatHistory,
    ThinkingBack,
    Jargon,
)

# GroupInfo 在某些版本可能不存在，尝试导入
try:
    from src.common.database.database_model import GroupInfo
    HAS_GROUP_INFO = True
except ImportError:
    GroupInfo = None
    HAS_GROUP_INFO = False

logger = get_logger("amnesia_plugin")


class AmnesiaCommand(BaseCommand):
    """失忆命令"""

    command_name = "amnesia"
    command_description = "让麦麦失忆忘记对话"
    command_pattern = r"(?P<amnesia_cmd>^/(失忆|忘记|断片|amnesia|forget|clear|清除上下文|清空上下文).*$)"

    # 类级别状态存储（所有实例共享）
    _pending_confirmations: ClassVar[Dict[str, dict]] = {}

    # 确认超时时间（秒）
    CONFIRM_TIMEOUT = 30

    async def execute(self) -> Tuple[bool, str, bool]:
        """执行命令"""
        # 权限检查
        if (
            not self.message
            or not self.message.message_info
            or not self.message.message_info.user_info
            or str(self.message.message_info.user_info.user_id) not in self.get_config("plugin.permission", [])
        ):
            return False, "没有权限", True

        # 获取用户ID
        user_id = str(self.message.message_info.user_info.user_id)

        command_text = self.matched_groups.get("amnesia_cmd", "").strip()
        parts = command_text.split()

        # 获取当前聊天ID
        chat_id = None
        if self.message and hasattr(self.message, 'chat_stream') and self.message.chat_stream:
            chat_id = self.message.chat_stream.stream_id

        if not chat_id:
            await self.send_text("😵 我脑子有点乱，不知道这是哪里...")
            return True, "无法获取聊天ID", True

        # 解析参数
        if len(parts) == 1:
            # 只有命令，显示帮助
            await self._show_help()
            return True, "显示帮助", True

        subcommand = parts[1] if len(parts) > 1 else ""

        if subcommand == "all" or subcommand == "全部":
            # 清除所有消息
            await self._forget_all(chat_id)
        elif subcommand == "total" or subcommand == "完全" or subcommand == "彻底":
            # 完全失忆 - 使用状态机确认
            await self._handle_total_amnesia(user_id, chat_id, parts)
        elif subcommand == "recent" or subcommand == "最近":
            # 清除最近N条消息
            count = int(parts[2]) if len(parts) > 2 else 10
            await self._forget_recent(chat_id, count)
        elif subcommand == "before" or subcommand == "之前":
            # 清除指定时间之前的消息
            hours = int(parts[2]) if len(parts) > 2 else 24
            await self._forget_before_hours(chat_id, hours)
        elif subcommand == "help" or subcommand == "帮助":
            await self._show_help()
        else:
            await self.send_text(f"🤨 {subcommand}？这是什么意思...\n使用 /失忆 帮助 查看用法")

        return True, "命令执行完成", True

    async def _handle_total_amnesia(self, user_id: str, chat_id: str, parts: list):
        """处理完全失忆命令（带状态机确认）"""

        # 定期清理过期的待确认请求（防止内存泄漏）
        self._cleanup_expired_confirmations()

        # 检查是否是确认命令
        is_confirm = len(parts) > 2 and parts[2] == "确认"

        if is_confirm:
            # === 确认阶段 ===
            pending = self._pending_confirmations.get(user_id)

            # 检查1：是否有待确认的请求
            if not pending:
                await self.send_text(
                    "❌ 没有待确认的失忆请求\n\n"
                    "请先发送 `/失忆 完全` 查看警告信息"
                )
                logger.warning(f"[失忆] 用户 {user_id} 尝试确认但无待确认请求")
                return

            # 检查2：是否超时
            elapsed = time.time() - pending["timestamp"]
            if elapsed > self.CONFIRM_TIMEOUT:
                del self._pending_confirmations[user_id]
                await self.send_text(
                    f"⏰ 确认已超时（{self.CONFIRM_TIMEOUT}秒）\n\n"
                    "请重新发送 `/失忆 完全` 发起新请求"
                )
                logger.info(f"[失忆] 用户 {user_id} 确认超时，已过 {elapsed:.1f} 秒")
                return

            # 检查3：聊天上下文是否匹配
            if pending["chat_id"] != chat_id:
                await self.send_text(
                    "❌ 请在发起请求的聊天中确认\n\n"
                    "您的请求发起于另一个聊天"
                )
                logger.warning(f"[失忆] 用户 {user_id} 在错误的聊天中确认: 期望 {pending['chat_id']}, 实际 {chat_id}")
                return

            # 所有检查通过，执行完全失忆
            del self._pending_confirmations[user_id]
            logger.info(f"[失忆] 用户 {user_id} 确认成功，执行完全失忆")
            await self._forget_total_confirmed()

        else:
            # === 发起请求阶段 ===

            # 检查是否已有待确认请求
            if user_id in self._pending_confirmations:
                old_pending = self._pending_confirmations[user_id]
                elapsed = time.time() - old_pending["timestamp"]
                remaining = max(0, self.CONFIRM_TIMEOUT - elapsed)

                if remaining > 0:
                    await self.send_text(
                        f"⚠️ 您已有一个待确认的失忆请求\n\n"
                        f"剩余确认时间: {remaining:.0f} 秒\n\n"
                        f"请发送 `/失忆 完全 确认` 或回复 `确认` 执行\n"
                        f"或等待超时后重新发起"
                    )
                    return
                else:
                    # 旧请求已超时，删除它
                    del self._pending_confirmations[user_id]

            # 创建新的待确认请求
            self._pending_confirmations[user_id] = {
                "timestamp": time.time(),
                "chat_id": chat_id,
            }

            logger.info(f"[失忆] 用户 {user_id} 发起完全失忆请求，等待确认")

            # 显示警告
            await self._forget_total()

    def _cleanup_expired_confirmations(self):
        """清理过期的待确认请求（超过5分钟）"""
        now = time.time()
        expired_users = [
            uid for uid, data in self._pending_confirmations.items()
            if now - data["timestamp"] > 300  # 5分钟
        ]
        for uid in expired_users:
            del self._pending_confirmations[uid]
            logger.debug(f"[失忆] 清理过期的确认请求: 用户 {uid}")

    async def _forget_all(self, chat_id: str):
        """忘记所有记忆"""
        try:
            # 获取消息总数
            count = Messages.select().where(Messages.chat_id == chat_id).count()

            if count == 0:
                await self.send_text("🤔 好像...我们之前没聊过天吧？")
                return

            # 删除所有消息
            deleted = Messages.delete().where(Messages.chat_id == chat_id).execute()

            # 发送回复（存储到数据库，稍后延迟删除）
            await self.send_text(f"💫 *咚* 诶...你们是谁？我怎么在这里？\n\n（麦麦忘记了所有事情，{deleted} 条记忆消失了）")
            logger.info(f"[失忆] 聊天 {chat_id} 的所有记忆已清除，共 {deleted} 条")

            # 启动延迟删除任务，清理回复消息
            import asyncio
            async def delayed_cleanup():
                await asyncio.sleep(3)  # 等待3秒
                # 删除所有消息（包括刚才的回复）
                final_deleted = Messages.delete().where(Messages.chat_id == chat_id).execute()
                logger.info(f"[失忆] 延迟清理完成，删除了 {final_deleted} 条消息（包括回复消息）")

            asyncio.create_task(delayed_cleanup())

        except Exception as e:
            logger.error(f"失忆失败: {e}", exc_info=True)
            await self.send_text(f"😖 失忆失败了...我脑子太好使了！\n\n错误: {str(e)}")

    async def _forget_recent(self, chat_id: str, count: int):
        """忘记最近的记忆"""
        try:
            # 获取最近的N条消息
            recent_messages = (Messages
                              .select()
                              .where(Messages.chat_id == chat_id)
                              .order_by(Messages.time.desc())
                              .limit(count))

            if not recent_messages:
                await self.send_text("🤔 咦...我们刚才说了什么吗？")
                return

            # 获取这些消息的ID
            message_ids = [msg.message_id for msg in recent_messages]

            # 删除这些消息
            deleted = Messages.delete().where(Messages.message_id.in_(message_ids)).execute()

            # 发送回复（存储到数据库，稍后延迟删除）
            await self.send_text(f"😵 诶？刚才发生了什么？\n\n（麦麦忘记了最近 {deleted} 条记忆）")
            logger.info(f"[失忆] 聊天 {chat_id} 的最近 {deleted} 条记忆已清除")

            # 启动延迟删除任务，清理回复消息和命令消息
            import asyncio
            async def delayed_cleanup():
                await asyncio.sleep(3)
                # 删除最新的2条消息（用户的命令 + 麦麦的回复）
                latest_messages = (Messages
                                  .select()
                                  .where(Messages.chat_id == chat_id)
                                  .order_by(Messages.time.desc())
                                  .limit(2))
                if latest_messages:
                    message_ids = [msg.message_id for msg in latest_messages]
                    deleted = Messages.delete().where(Messages.message_id.in_(message_ids)).execute()
                    logger.info(f"[失忆] 延迟清理完成，删除了 {deleted} 条消息（命令+回复）")

            asyncio.create_task(delayed_cleanup())

        except Exception as e:
            logger.error(f"失忆失败: {e}", exc_info=True)
            await self.send_text(f"😖 诶...好像忘不掉！\n\n错误: {str(e)}")

    async def _forget_before_hours(self, chat_id: str, hours: int):
        """忘记很久以前的记忆"""
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
                await self.send_text(f"🤔 {hours} 小时前？那时候我们有聊过吗...")
            else:
                # 发送回复（存储到数据库，稍后延迟删除）
                await self.send_text(f"😌 嗯...{hours} 小时前的事都是浮云~\n\n（麦麦忘记了 {deleted} 条久远的记忆）")
                logger.info(f"[失忆] 聊天 {chat_id} 的 {hours} 小时前记忆已清除，共 {deleted} 条")

                # 启动延迟删除任务，清理回复消息和命令消息
                import asyncio
                async def delayed_cleanup():
                    await asyncio.sleep(3)
                    # 删除最新的2条消息（用户的命令 + 麦麦的回复）
                    latest_messages = (Messages
                                      .select()
                                      .where(Messages.chat_id == chat_id)
                                      .order_by(Messages.time.desc())
                                      .limit(2))
                    if latest_messages:
                        message_ids = [msg.message_id for msg in latest_messages]
                        deleted = Messages.delete().where(Messages.message_id.in_(message_ids)).execute()
                        logger.info(f"[失忆] 延迟清理完成，删除了 {deleted} 条消息（命令+回复）")

                asyncio.create_task(delayed_cleanup())

        except Exception as e:
            logger.error(f"失忆失败: {e}", exc_info=True)
            await self.send_text(f"😖 这些记忆好像太深刻了，忘不掉！\n\n错误: {str(e)}")

    async def _forget_total(self):
        """完全失忆 - 清除所有记忆数据"""
        try:
            # 发送警告（也会被延迟删除）
            await self.send_text(
                "⚠️ **危险警告！**\n\n"
                "这将清除麦麦的**所有记忆**，包括：\n"
                "• 所有聊天记录\n"
                "• 认识的所有人和印象\n"
                "• 群组信息和印象\n"
                "• 学习到的表达风格\n"
                "• 动作记录\n"
                "• 聊天历史概括\n"
                "• 思考记录\n"
                "• 俚语黑话学习\n\n"
                "💥 **这是不可逆的操作！**\n\n"
                f"如果确认，请在 **{self.CONFIRM_TIMEOUT}秒内**：\n"
                "• 发送 `/失忆 完全 确认`\n"
                "• 或直接回复 `确认`"
            )

            return

        except Exception as e:
            logger.error(f"完全失忆警告失败: {e}", exc_info=True)
            await self.send_text(f"😖 发生错误: {str(e)}")

    async def _forget_total_confirmed(self):
        """执行完全失忆（已确认）"""
        try:
            from src.common.database.database import db

            stats = {
                "messages": 0,
                "chat_streams": 0,
                "person_info": 0,
                "group_info": 0,
                "expression": 0,
                "action_records": 0,
                "chat_history": 0,
                "thinking_back": 0,
                "jargon": 0,
            }

            # 1. 清除数据库表
            logger.info("[完全失忆] 开始清除数据库...")

            stats["messages"] = Messages.delete().execute()
            stats["chat_streams"] = ChatStreams.delete().execute()
            stats["person_info"] = PersonInfo.delete().execute()

            # GroupInfo 表可能不存在，检查后清除
            if HAS_GROUP_INFO and GroupInfo is not None:
                try:
                    if db.table_exists(GroupInfo):
                        stats["group_info"] = GroupInfo.delete().execute()
                    else:
                        logger.warning("[完全失忆] GroupInfo 表不存在，跳过清除")
                except Exception as e:
                    logger.warning(f"[完全失忆] 清除 GroupInfo 失败: {e}")
            else:
                logger.info("[完全失忆] GroupInfo 模型未导入，跳过清除")

            stats["expression"] = Expression.delete().execute()
            stats["action_records"] = ActionRecords.delete().execute()

            # 清除长期记忆和学习数据
            stats["chat_history"] = ChatHistory.delete().execute()
            stats["thinking_back"] = ThinkingBack.delete().execute()
            stats["jargon"] = Jargon.delete().execute()

            # 2. 清除本地存储（保留统计数据）
            logger.info("[完全失忆] 清除本地存储...")
            local_store_path = "/home/ubuntu/maimai/MaiBot/data/local_store.json"
            if os.path.exists(local_store_path):
                # 读取现有数据
                with open(local_store_path, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)

                # 重置为初始状态，但保留统计数据
                initial_data = {
                    "deploy_time": datetime.now().timestamp(),
                    "mmc_uuid": old_data.get("mmc_uuid", "")
                }

                # 保留统计数据（如果存在）
                if "last_full_statistics" in old_data:
                    initial_data["last_full_statistics"] = old_data["last_full_statistics"]
                    logger.info("[完全失忆] 保留统计数据")

                with open(local_store_path, 'w', encoding='utf-8') as f:
                    json.dump(initial_data, f, indent=4, ensure_ascii=False)

            # 3. 清除表达风格学习
            logger.info("[完全失忆] 清除表达风格学习...")
            expression_path = "/home/ubuntu/maimai/MaiBot/data/expression"
            if os.path.exists(expression_path):
                for subdir in ["learnt_grammar", "learnt_style"]:
                    subdir_path = os.path.join(expression_path, subdir)
                    if os.path.exists(subdir_path):
                        shutil.rmtree(subdir_path)
                        os.makedirs(subdir_path)

            # 生成报告
            total_cleared = sum([
                stats["messages"],
                stats["chat_streams"],
                stats["person_info"],
                stats["group_info"],
                stats["expression"],
                stats["action_records"],
                stats["chat_history"],
                stats["thinking_back"],
                stats["jargon"],
            ])

            # 简化报告：只显示总计，避免消息过长被截断
            # 这个报告消息会被延迟删除清理
            report = f"""💫✨ *完全失忆完成* ✨💫

我...我是谁？这里是哪里？

📊 清除了 {total_cleared} 项记忆数据

麦麦现在是全新的状态了...
一切都要重新开始呢~ 💫"""

            await self.send_text(report)

            # 详细统计记录到日志
            logger.info(f"[完全失忆] 完成！详细统计：")
            logger.info(f"  💬 对话消息: {stats['messages']} 条")
            logger.info(f"  🌊 聊天流: {stats['chat_streams']} 个")
            logger.info(f"  👤 个人印象: {stats['person_info']} 人")
            logger.info(f"  👥 群组记忆: {stats['group_info']} 个")
            logger.info(f"  💭 表达风格: {stats['expression']} 个")
            logger.info(f"  🎬 动作记录: {stats['action_records']} 条")
            logger.info(f"  📚 聊天历史概括: {stats['chat_history']} 条")
            logger.info(f"  🧠 思考记录: {stats['thinking_back']} 条")
            logger.info(f"  🗣️ 俚语黑话: {stats['jargon']} 条")
            logger.info(f"  📦 总计: {total_cleared} 项记忆")

            # 分两步删除消息
            # 第一步：等待统计报告被存储后立即删除
            await asyncio.sleep(0.5)  # 短暂等待确保统计报告被存储
            msg_count = Messages.delete().execute()
            stream_count = ChatStreams.delete().execute()
            logger.info(f"[完全失忆] 第一次清除: {msg_count} 条消息, {stream_count} 个聊天流")

            # 第二步：启动后台任务延迟删除，确保用户命令消息也被清除
            # 因为用户命令消息是在插件返回后才被存储的
            async def delayed_delete():
                await asyncio.sleep(5)  # 等待5秒
                # 直接删除所有消息，完全失忆就是要清空一切
                deleted_msg = Messages.delete().execute()
                deleted_stream = ChatStreams.delete().execute()
                logger.info(f"[完全失忆] 延迟删除完成: {deleted_msg} 条消息, {deleted_stream} 个聊天流")
                logger.info(f"[完全失忆] 数据库已完全清空，真正的失忆完成！")

            # 启动后台任务
            asyncio.create_task(delayed_delete())
            logger.info("[完全失忆] 已启动延迟删除任务，将在5秒后再次清除")

        except Exception as e:
            logger.error(f"完全失忆失败: {e}", exc_info=True)
            await self.send_text(f"😖 完全失忆失败了...\n\n错误: {str(e)}")


    async def _show_help(self):
        """显示帮助"""
        help_text = """💫 麦麦失忆插件

🔐 需要管理员权限才能让麦麦失忆哦~

用法:
/失忆 全部 - 忘记当前聊天的所有记忆
/失忆 完全 - ⚠️ 完全失忆（清除所有数据，需二次确认）
/失忆 最近 [数量] - 忘记最近N条记忆（默认10条）
/失忆 之前 [小时] - 忘记N小时前的记忆（默认24小时）
/失忆 帮助 - 显示此帮助

💥 完全失忆模式：
这是最彻底的失忆，会清除：
• 所有聊天记录和会话
• 认识的所有人和印象
• 群组信息和印象
• 学习到的表达风格
• 动作记录
• 聊天历史概括
• 思考记录
• 俚语黑话学习

使用步骤：
1. 发送 /失忆 完全
2. 查看警告信息
3. 在30秒内确认（两种方式）：
   • 发送 /失忆 完全 确认
   • 或直接回复 确认

多种命令都可以用:
/clear all          # 清除当前聊天的所有记忆
/forget total       # 完全失忆（英文）
/amnesia total      # 健忘症模式
/失忆 完全           # 完全失忆（中文）

示例:
/clear all              # 清除当前聊天的所有记忆
/失忆 全部              # 当前聊天完全断片
/失忆 完全              # ⚠️ 所有数据完全失忆
/失忆 最近 20          # 刚才说了啥？忘了
/clear before 48       # 清除48小时前的消息

⚠️ 警告:
• "全部" 只清除当前聊天，其他聊天不受影响
• "完全" 清除所有数据，无法恢复！
• 确认有30秒超时限制

权限配置:
管理员可在 plugins/amnesia_plugin/config.toml 中配置
permission = ["用户ID1", "用户ID2"]"""

        await self.send_text(help_text)


class AmnesiaConfirmHandler(BaseEventHandler):
    """监听"确认"消息的事件处理器"""

    handler_name = "amnesia_confirm_handler"
    handler_description = "监听失忆确认消息"
    event_type = EventType.ON_MESSAGE
    intercept_message = True  # 拦截消息，不让后续处理
    weight = 100  # 高优先级

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], None, Optional[MaiMessages]]:
        """检查是否是确认消息"""
        from src.common.database.database import db

        if message is None:
            return True, True, None, None, None

        # 获取消息内容和用户信息
        text = (message.plain_text or "").strip()
        user_id = str(message.message_base_info.get("user_id", ""))
        chat_id = message.stream_id

        # 只处理"确认"消息
        if text != "确认":
            return True, True, None, None, None  # 不拦截，继续处理

        # 检查是否有待确认请求（使用 AmnesiaCommand 的共享状态）
        pending = AmnesiaCommand._pending_confirmations.get(user_id)
        if not pending:
            return True, True, None, None, None  # 不拦截

        # 检查超时
        if time.time() - pending["timestamp"] > AmnesiaCommand.CONFIRM_TIMEOUT:
            del AmnesiaCommand._pending_confirmations[user_id]
            return True, True, None, None, None  # 不拦截

        # 检查聊天匹配
        if pending["chat_id"] != chat_id:
            return True, True, None, None, None  # 不拦截

        # === 确认成功，执行完全失忆 ===
        del AmnesiaCommand._pending_confirmations[user_id]
        logger.info(f"[失忆] 用户 {user_id} 通过回复'确认'执行完全失忆")

        # 执行完全失忆
        await self._execute_total_amnesia(chat_id)

        # 返回：成功, 不继续处理(拦截), 消息, 自定义结果, 修改后的消息
        return True, False, "确认成功，已执行完全失忆", None, None

    async def _execute_total_amnesia(self, stream_id: str):
        """执行完全失忆"""
        from src.common.database.database import db

        try:
            stats = {
                "messages": 0,
                "chat_streams": 0,
                "person_info": 0,
                "group_info": 0,
                "expression": 0,
                "action_records": 0,
                "chat_history": 0,
                "thinking_back": 0,
                "jargon": 0,
            }

            # 1. 清除数据库表
            logger.info("[完全失忆] 开始清除数据库...")

            stats["messages"] = Messages.delete().execute()
            stats["chat_streams"] = ChatStreams.delete().execute()
            stats["person_info"] = PersonInfo.delete().execute()

            # GroupInfo 表可能不存在，检查后清除
            if HAS_GROUP_INFO and GroupInfo is not None:
                try:
                    if db.table_exists(GroupInfo):
                        stats["group_info"] = GroupInfo.delete().execute()
                    else:
                        logger.warning("[完全失忆] GroupInfo 表不存在，跳过清除")
                except Exception as e:
                    logger.warning(f"[完全失忆] 清除 GroupInfo 失败: {e}")
            else:
                logger.info("[完全失忆] GroupInfo 模型未导入，跳过清除")

            stats["expression"] = Expression.delete().execute()
            stats["action_records"] = ActionRecords.delete().execute()

            # 清除长期记忆和学习数据
            stats["chat_history"] = ChatHistory.delete().execute()
            stats["thinking_back"] = ThinkingBack.delete().execute()
            stats["jargon"] = Jargon.delete().execute()

            # 2. 清除本地存储（保留统计数据）
            logger.info("[完全失忆] 清除本地存储...")
            local_store_path = "/home/ubuntu/maimai/MaiBot/data/local_store.json"
            if os.path.exists(local_store_path):
                with open(local_store_path, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)

                initial_data = {
                    "deploy_time": datetime.now().timestamp(),
                    "mmc_uuid": old_data.get("mmc_uuid", "")
                }

                if "last_full_statistics" in old_data:
                    initial_data["last_full_statistics"] = old_data["last_full_statistics"]
                    logger.info("[完全失忆] 保留统计数据")

                with open(local_store_path, 'w', encoding='utf-8') as f:
                    json.dump(initial_data, f, indent=4, ensure_ascii=False)

            # 3. 清除表达风格学习
            logger.info("[完全失忆] 清除表达风格学习...")
            expression_path = "/home/ubuntu/maimai/MaiBot/data/expression"
            if os.path.exists(expression_path):
                for subdir in ["learnt_grammar", "learnt_style"]:
                    subdir_path = os.path.join(expression_path, subdir)
                    if os.path.exists(subdir_path):
                        shutil.rmtree(subdir_path)
                        os.makedirs(subdir_path)

            # 生成报告
            total_cleared = sum(stats.values())

            report = f"""💫✨ *完全失忆完成* ✨💫

我...我是谁？这里是哪里？

📊 清除了 {total_cleared} 项记忆数据

麦麦现在是全新的状态了...
一切都要重新开始呢~ 💫"""

            await self.send_text(stream_id, report)

            # 详细统计记录到日志
            logger.info(f"[完全失忆] 完成！详细统计：")
            logger.info(f"  💬 对话消息: {stats['messages']} 条")
            logger.info(f"  🌊 聊天流: {stats['chat_streams']} 个")
            logger.info(f"  👤 个人印象: {stats['person_info']} 人")
            logger.info(f"  👥 群组记忆: {stats['group_info']} 个")
            logger.info(f"  💭 表达风格: {stats['expression']} 个")
            logger.info(f"  🎬 动作记录: {stats['action_records']} 条")
            logger.info(f"  📚 聊天历史概括: {stats['chat_history']} 条")
            logger.info(f"  🧠 思考记录: {stats['thinking_back']} 条")
            logger.info(f"  🗣️ 俚语黑话: {stats['jargon']} 条")
            logger.info(f"  📦 总计: {total_cleared} 项记忆")

            # 延迟删除
            await asyncio.sleep(0.5)
            msg_count = Messages.delete().execute()
            stream_count = ChatStreams.delete().execute()
            logger.info(f"[完全失忆] 第一次清除: {msg_count} 条消息, {stream_count} 个聊天流")

            async def delayed_delete():
                await asyncio.sleep(5)
                # 直接删除所有消息，完全失忆就是要清空一切
                deleted_msg = Messages.delete().execute()
                deleted_stream = ChatStreams.delete().execute()
                logger.info(f"[完全失忆] 延迟删除完成: {deleted_msg} 条消息, {deleted_stream} 个聊天流")
                logger.info(f"[完全失忆] 数据库已完全清空，真正的失忆完成！")

            asyncio.create_task(delayed_delete())
            logger.info("[完全失忆] 已启动延迟删除任务，将在5秒后再次清除")

        except Exception as e:
            logger.error(f"完全失忆失败: {e}", exc_info=True)
            await self.send_text(stream_id, f"😖 完全失忆失败了...\n\n错误: {str(e)}")


@register_plugin
class AmnesiaPlugin(BasePlugin):
    """麦麦失忆插件"""

    plugin_name: str = "amnesia_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用麦麦失忆插件"
            ),
            "config_version": ConfigField(
                type=str,
                default="1.5.0",
                description="配置文件版本"
            ),
            "permission": ConfigField(
                type=list,
                default=["1334431750"],
                description="有权限让麦麦失忆的用户ID列表（管理员）"
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple]:
        """获取插件组件"""
        return [
            (AmnesiaCommand.get_command_info(), AmnesiaCommand),
            (AmnesiaConfirmHandler.get_handler_info(), AmnesiaConfirmHandler),
        ]
