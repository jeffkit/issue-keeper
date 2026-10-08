"""tests 必须是常规包（不能依赖 PEP 420 命名空间包）。

机器上残留的 editable install（如 _editable_impl_tunely.pth）会把别的仓的
tests/（常规包，有 __init__.py）插进 sys.path，遮蔽本仓的命名空间 tests，
导致 `from tests.test_x import ...` 收集期 ModuleNotFoundError。
有 __init__.py 后，cwd/rootdir 下的真实包优先命中。
"""
