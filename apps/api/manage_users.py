"""Manage invitation-only username/password accounts without a web admin API."""

import argparse
import getpass
import random
import secrets
import sys

from app.auth.password import PasswordCredentialService
from app.auth.password_escrow import PasswordEscrowStore
from app.core.config import settings
from app.infrastructure.database import build_database


DEMO_USERNAME_PREFIX = "slow-demo"
DEMO_USERNAME_ATTEMPTS = 20
PASSWORD_LENGTH = 24
PASSWORD_LOWERCASE = "abcdefghijkmnopqrstuvwxyz"
PASSWORD_UPPERCASE = "ABCDEFGHJKLMNPQRSTUVWXYZ"
PASSWORD_DIGITS = "23456789"
PASSWORD_SYMBOLS = "!@#$%^&*()-_=+"


def generated_password() -> str:
    """Return a copy-friendly password containing every required character class."""

    alphabet = (
        PASSWORD_LOWERCASE
        + PASSWORD_UPPERCASE
        + PASSWORD_DIGITS
        + PASSWORD_SYMBOLS
    )
    characters = [
        secrets.choice(PASSWORD_LOWERCASE),
        secrets.choice(PASSWORD_UPPERCASE),
        secrets.choice(PASSWORD_DIGITS),
        secrets.choice(PASSWORD_SYMBOLS),
    ]
    characters.extend(
        secrets.choice(alphabet)
        for _ in range(PASSWORD_LENGTH - len(characters))
    )
    random.SystemRandom().shuffle(characters)
    return "".join(characters)


def generated_demo_username() -> str:
    return f"{DEMO_USERNAME_PREFIX}{secrets.randbelow(100_000):05d}"


def create_demo_account(
    service: PasswordCredentialService,
    *,
    display_name: str | None = None,
):
    """Create a random demo account, retrying the small username namespace."""

    for _ in range(DEMO_USERNAME_ATTEMPTS):
        username = generated_demo_username()
        password = generated_password()
        try:
            user = service.create_account(
                username=username,
                display_name=display_name or username,
                password=password,
            )
        except ValueError as error:
            if str(error) == "账号已存在":
                continue
            raise
        return user, username, password
    raise RuntimeError("连续生成的演示账号均已存在，请重试")


def selected_password(arguments: argparse.Namespace) -> str:
    if arguments.password_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        if not value:
            raise ValueError("标准输入中没有密码")
        return value
    if arguments.prompt_password:
        first = getpass.getpass("新密码：")
        second = getpass.getpass("再次输入：")
        if first != second:
            raise ValueError("两次输入的密码不一致")
        return first
    return generated_password()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="创建、禁用和重置 Slow 内测账号",
    )
    commands = result.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="创建受邀账号")
    create.add_argument("username")
    create.add_argument("--name", required=True, help="用户显示名称")
    create_password = create.add_mutually_exclusive_group()
    create_password.add_argument("--prompt-password", action="store_true")
    create_password.add_argument("--password-stdin", action="store_true")

    create_demo = commands.add_parser(
        "create-demo",
        help="自动生成 slow-demo 加五位数字的受邀账号",
    )
    create_demo.add_argument(
        "--name",
        help="用户显示名称；默认与随机账号相同",
    )

    for command, help_text in (
        ("disable", "禁用账号并撤销全部会话"),
        ("enable", "重新启用账号"),
    ):
        item = commands.add_parser(command, help=help_text)
        item.add_argument("username")

    reset = commands.add_parser(
        "reset-password",
        help="重置密码并撤销全部会话",
    )
    reset.add_argument("username")
    reset_password = reset.add_mutually_exclusive_group()
    reset_password.add_argument("--prompt-password", action="store_true")
    reset_password.add_argument("--password-stdin", action="store_true")

    show = commands.add_parser(
        "show-password",
        help="查看显式托管的开发期密码",
    )
    show.add_argument("username")

    purge = commands.add_parser(
        "purge-passwords",
        help="关闭托管前删除全部明文密码记录",
    )
    purge.add_argument(
        "--confirm",
        action="store_true",
        help="确认删除全部托管密码",
    )
    return result


def main() -> int:
    arguments = parser().parse_args()
    engine, sessions = build_database(settings.database_url)
    try:
        escrow = PasswordEscrowStore(
            settings.password_escrow_path,
            enabled=settings.password_escrow_enabled,
            app_mode=settings.app_mode,
        )
        if arguments.command == "show-password":
            password = escrow.reveal(username=arguments.username)
            print(f"账号：{arguments.username}")
            print(f"托管密码：{password}")
            return 0
        if arguments.command == "purge-passwords":
            if not arguments.confirm:
                raise ValueError("删除全部托管密码需要使用 --confirm")
            removed = escrow.purge()
            print("已删除全部托管密码" if removed else "没有密码托管文件")
            return 0
        with sessions() as db:
            service = PasswordCredentialService(db)
            if arguments.command == "create":
                password = selected_password(arguments)
                user = service.create_account(
                    username=arguments.username,
                    display_name=arguments.name,
                    password=password,
                )
                if escrow.enabled:
                    escrow.record(username=arguments.username, password=password)
                print(f"已创建账号：{arguments.username}（{user.name}）")
                print(f"初始密码：{password}")
                if escrow.enabled:
                    print("该密码已写入显式开发期托管，可用 show-password 查看。")
                else:
                    print("请通过私密渠道发送；系统不会再次显示该密码。")
            elif arguments.command == "create-demo":
                user, username, password = create_demo_account(
                    service,
                    display_name=arguments.name,
                )
                print(f"已创建演示账号：{username}（{user.name}）")
                print(f"账号：{username}")
                print(f"初始密码：{password}")
                print("账号已写入数据库；请立即通过私密渠道发送以上凭据。")
                print("系统只保存 Argon2id 密码哈希，不会再次显示该密码。")
            elif arguments.command == "disable":
                service.set_account_enabled(
                    username=arguments.username,
                    enabled=False,
                )
                print(f"已禁用账号并撤销全部会话：{arguments.username}")
            elif arguments.command == "enable":
                service.set_account_enabled(
                    username=arguments.username,
                    enabled=True,
                )
                print(f"已启用账号：{arguments.username}")
            else:
                password = selected_password(arguments)
                service.reset_password(
                    username=arguments.username,
                    password=password,
                )
                if escrow.enabled:
                    escrow.record(username=arguments.username, password=password)
                print(f"已重置密码并撤销全部会话：{arguments.username}")
                print(f"新密码：{password}")
                if escrow.enabled:
                    print("该密码已写入显式开发期托管，可用 show-password 查看。")
                else:
                    print("请通过私密渠道发送；系统不会再次显示该密码。")
    except (ValueError, RuntimeError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
