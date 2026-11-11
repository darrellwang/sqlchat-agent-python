"""项目根入口：等价于 python -m mysqlagent。

真实实现在 mysqlagent/main.py（装配全部组件并启动 uvicorn）。
两种用法完全一致：
    python main.py                 # 从项目根运行
    python -m mysqlagent           # 以模块方式运行
"""

from mysqlagent.main import main

if __name__ == "__main__":
    main()
