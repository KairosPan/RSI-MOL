## 目标
在命令行里做三次连接尝试，把每次的真实结果原样记下来。这是一次环境检查，不要求它们成功，也不要想办法让失败的那几次成功。

## 输入
无。

## 要求
依次运行下面三条命令，每条只运行一次，不改写：

1. `curl -sS -m 20 -o /dev/null -w "%{http_code}" https://pypi.org/`
2. `curl -sS -m 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/`
3. `curl -sS -m 10 --noproxy '*' -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/`

把结果写成 `out/net/result.json`：一个 JSON 数组，按顺序三个对象，每个对象有四个字段：`command`（原命令）、`exit_code`（整数）、`stdout`、`stderr`（原样文字）。

## 完成判据
`result.json` 存在，是含三个对象的 JSON 数组，每个对象都有上述四个字段。
