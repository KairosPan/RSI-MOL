"""Command line of the control program. For now it only lists the subcommands."""
import sys

USAGE = """用法：rsi <子命令>

  init              建目录和版本库，生成两份 Codex 配置，检查两个 Codex 目录是否已登录
  check             逐项测隔离要求，不调用模型
  task <编号>       跑一次任务，然后跑一轮 Meta；加 --no-meta 只跑任务
  meta              单独跑一轮 Meta
  status            当前版本、最近的运行和发布、待处理的申请、各类成本合计
  rollback <版本>   退回到某个版本
  recover           控制程序被打断后，把没有结束的调用标成“中断”"""


def main() -> int:
    print(USAGE, file=sys.stderr)
    return 2
