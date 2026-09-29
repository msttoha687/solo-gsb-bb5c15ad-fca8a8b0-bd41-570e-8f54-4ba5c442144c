"""两个历史版本地址用途对比接口的端到端测试。

直接运行：``python3 -m tests.test_version_diff``（或 pytest 收集）。

覆盖：
* 纯读取：数据全部取自不可变快照表，调用前后池版本与状态不变；
* 任一端口版本不存在（含 0、负数、未来版本）返回 404，不以当前池
  状态代替缺失快照；非整数版本号返回 422；
* 保留 / 已分配各自的 added/removed 按地址集合求差，方向语义正确；
* 差集折叠为数量最少的规范 CIDR（相邻记录合并），并按网络地址、
  前缀长度排序；
* 记录拆分 / 合并且覆盖范围不变时四个列表均为空，不产生伪变化；
* 同一版本自比四个列表均为空；起止版本号允许逆序。
"""
from __future__ import annotations

import ipaddress
import os
import tempfile

PARENT = "10.10.0.0/16"
RESERVED = "10.10.0.0/20,10.10.255.0/24"

os.environ["PARENT_CIDR"] = PARENT
os.environ["RESERVED_CIDRS"] = RESERVED
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_version_diff.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.allocator import _set_diff_cidrs  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _preview(prefixes):
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": prefixes}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True, body
    return body


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reclaim(version, cidr):
    r = client.post(
        "/api/allocations/reclaim",
        json={"pool_version": version, "cidr": cidr},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _replace(version, desired):
    r = client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": desired},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _diff(a, b):
    r = client.get(f"/api/pool/versions/{a}/diff/{b}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_version"] == a
    assert body["to_version"] == b
    return body


def _nets(cidrs):
    return [ipaddress.ip_network(c) for c in cidrs]


def _assert_sorted(cidrs):
    nets = _nets(cidrs)
    assert nets == sorted(nets, key=lambda n: (int(n.network_address), n.prefixlen))


def _unit_diff(base, subtract):
    return [
        str(n)
        for n in _set_diff_cidrs(_nets(base), _nets(subtract), 32)
    ]


def main():
    # ---- 集合求差工具：相邻折叠为最少 CIDR、扣除与排序 ----
    assert _unit_diff(
        ["10.0.0.0/25", "10.0.0.128/26", "10.0.0.192/26"], []
    ) == ["10.0.0.0/24"]
    assert _unit_diff(["10.0.0.0/24"], ["10.0.0.0/25"]) == [
        "10.0.0.128/25"
    ]
    # 扣除后不相邻的两部分各自折叠，按网络地址排序。
    assert _unit_diff(
        ["10.0.0.0/24", "10.0.1.0/24"], ["10.0.0.128/25"]
    ) == ["10.0.0.0/25", "10.0.1.0/24"]
    # 有间隔不合并。
    assert _unit_diff(["10.0.0.0/25", "10.0.2.0/24"], []) == [
        "10.0.0.0/25",
        "10.0.2.0/24",
    ]
    print("set-diff folding unit cases ok")

    # ---- 造版本：v1 基线；提交两个相邻 /24 -> v2 ----
    assert client.get("/api/pool").json()["version"] == 1
    a1, a2 = _preview([24, 24])["allocations"]
    # 两个分配恰好相邻，覆盖一个 /23（推导期望折叠结果用）。
    merged = list(
        ipaddress.collapse_addresses(_nets([a1, a2]))
    )
    assert len(merged) == 1, (a1, a2)
    merged_cidr = str(merged[0])
    committed = _commit(1, [a1, a2])
    assert committed["pool_version"] == 2

    d = _diff(1, 2)
    assert d["reserved"] == {"added": [], "removed": []}
    # 两条相邻 /24 的差集折叠为一个最少的 /23，而非逐记录罗列。
    assert d["allocated"] == {"added": [merged_cidr], "removed": []}
    _assert_sorted(d["allocated"]["added"])
    print("v1 -> v2: allocated added folded to", merged_cidr)

    # ---- 回收整块 -> v3 已分配为空；反向差集 ----
    _reclaim(2, merged_cidr)
    d = _diff(2, 3)
    assert d["allocated"] == {"added": [], "removed": [merged_cidr]}
    print("v2 -> v3: allocated removed folded ok")

    # ---- 以一条 /23 重新提交同覆盖范围 -> v4：记录不同、集合相同 ----
    a23 = _preview([23])["allocations"]
    assert a23 == [merged_cidr], a23
    _commit(3, a23)
    d = _diff(2, 4)
    # v2 为两条 /24、v4 为一条 /23，覆盖地址集合完全相同，无伪变化。
    assert d["reserved"] == {"added": [], "removed": []}
    assert d["allocated"] == {"added": [], "removed": []}, d
    print("v2 -> v4: record merge without coverage change -> no diff ok")

    # ---- 回收 /23 的前半 -> v5 只剩后半 /24：拆分不产生伪变化 ----
    half = ipaddress.ip_network(merged_cidr).subnets()
    first_half, second_half = (str(n) for n in half)
    v5 = _reclaim(4, first_half)
    assert v5["allocated_cidrs"] == [second_half], v5
    d = _diff(4, 5)
    # 后半 /24 在两版本中都被占用（v4 时是 /23 的一部分），不得出现在
    # added/removed 中；只有真正被回收的前半 /24 是 removed。
    assert d["allocated"] == {"added": [], "removed": [first_half]}, d
    d = _diff(5, 4)
    assert d["allocated"] == {"added": [first_half], "removed": []}, d
    print("v4 <-> v5: partial reclaim split diff ok")

    # ---- 保留区替换 -> v6：保留差集与已分配差集互不串扰 ----
    new_reserved = ["10.10.200.0/24", "10.10.201.0/24"]  # 相邻 -> /23
    replaced = _replace(5, new_reserved)
    assert replaced["reserved_cidrs"] == ["10.10.200.0/23"]
    diff_1_6 = _diff(1, 6)
    assert diff_1_6["reserved"]["added"] == ["10.10.200.0/23"]
    # 移出的两段保留按网络地址升序：10.10.0.0/20 在 10.10.255.0/24 前。
    assert diff_1_6["reserved"]["removed"] == [
        "10.10.0.0/20",
        "10.10.255.0/24",
    ], diff_1_6["reserved"]
    _assert_sorted(diff_1_6["reserved"]["removed"])
    # 已分配用途独立比较：v1 为空、v6 仍占用后半 /24。
    assert diff_1_6["allocated"] == {
        "added": [second_half],
        "removed": [],
    }
    # 反向：added/removed 严格互换。
    rev = _diff(6, 1)
    assert rev["reserved"]["added"] == diff_1_6["reserved"]["removed"]
    assert rev["reserved"]["removed"] == diff_1_6["reserved"]["added"]
    assert rev["allocated"]["added"] == []
    assert rev["allocated"]["removed"] == [second_half]
    print("v1 <-> v6: reserved/allocated independent diffs ok")

    # ---- 同版本自比：四个列表全空（含内容非空的版本）----
    for v in (1, 2, 4, 6):
        d = _diff(v, v)
        assert d == {
            "from_version": v,
            "to_version": v,
            "reserved": {"added": [], "removed": []},
            "allocated": {"added": [], "removed": []},
        }, d
    print("same-version diff all empty ok")

    # ---- 任一版本不存在 -> 404（不用当前池状态代替）----
    for a, b in ((1, 999), (999, 1), (0, 1), (1, -1), (998, 999)):
        r = client.get(f"/api/pool/versions/{a}/diff/{b}")
        assert r.status_code == 404, (a, b, r.status_code, r.text)
        assert "unknown pool version" in r.json()["detail"]
    # 非整数版本号 -> 422。
    assert client.get("/api/pool/versions/abc/diff/2").status_code == 422
    assert client.get("/api/pool/versions/1/diff/xyz").status_code == 422
    print("missing version -> 404, non-integer -> 422 ok")

    # ---- 纯只读：全部对比调用后池版本与状态不变 ----
    pool = client.get("/api/pool").json()
    assert pool["version"] == 6
    assert pool["reserved_cidrs"] == ["10.10.200.0/23"]
    assert pool["allocated_cidrs"] == [second_half]
    # 历史快照同样未被触碰：再调一次结果与此前 v1->v6 完全一致。
    assert _diff(1, 6) == diff_1_6
    assert client.get("/api/pool").json()["version"] == 6
    print("diff endpoint is read-only ok")

    print("ALL VERSION DIFF TESTS PASSED")


if __name__ == "__main__":
    main()
