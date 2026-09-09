# 阶段5：数值桥接发布适配与本机验证

后续补充（实施前`edbc5a6`）：用户要求新增依赖进入requirements，现将4项固定依赖追加到原`release/requirements-build.txt`，并调整脚本的PyInstaller单条声明定位。下方原阶段5“单行文件”为当时事实，不改写历史测试；第4节安装命令已更新为当前入口，本次补充验证见其独立提交正文。

## 1. 范围与身份

- 日期：2026-09-09；实施前提交：`111ac6775a326b602a508089244d31c4bb4b615d`。
- 仓库：`APSGOV7`；分支：`codex/solver-performance-optimization`。
- 本机：macOS ARM64；解释器：`/Users/miles/anaconda3/envs/aps_3.10.18/bin/python`，Python3.10.18。
- 前阶段已完成五对完整对照和原质量，证据保留在[阶段4](../stage_04_comparison/README.md)。本阶段不重跑或改写这些样本。
- 修改仅包含三个原发布脚本、发布说明、两份发布测试及本专项文档。没有改生产算法、依赖声明、规则、候选预算、服务接口、YAML或SQLite。

**当前只交付发布适配源码及本机可运行检查；Windows EXE尚未构建，真实权限/运行库可用性及现场订单仍待验。** 不将阶段5整阶段或阶段6标为完成。

## 2. 已落实的检查

| 位置 | 本次内容 |
|---|---|
| `release/build_exe.ps1` | 仅通过包元数据读取和精确核验NumPy2.2.6、Numba0.65.1、llvmlite0.47.0、hooks2026.6；多行Python仍走标准输入；缺依赖显示手工安装命令，不自动安装 |
| `release/verify_release.py` | 清单增加四项实际版本；缺字段/漂移明确失败；原源码/测试文件保护保留，另禁止 `.nbi/.nbc` 编译缓存 |
| `release/smoke_release.py` | 复用月计划API/规则API、诊断和启停辅助；正常可写目录检查后，运行受控单桥/双桥、重复和重启场景 |
| 发布测试 | 真实源码接口求解与数值编译执行；另用模拟命令检查Windows权限恢复、临时路径、独占证据写入和外部Python环境移除 |

保持PyInstaller6.22.2、单行`requirements-build.txt`、现有目录式构建参数和hooks；没有新增规格文件、通用依赖收集或源码放行名单。版本读取不导入Numba，也不触发即时编译。

### 2.1 受控输入和预期

使用临时库，经现有保存并启用接口设置代码已有的标准GQGA4规则和原型；不是修改正式种子。两个普通真实DC01订单各600吨、宽1000毫米、温区700～800，同一个期间P0。字典必须实际命中，缺项直接失败，不猜类别。

| 场景 | 真实厚度 | 预期生产顺序（厚度） | 节点/总重 | 虚拟重量 |
|---|---|---|---|---|
| 单桥 | 0.6、1.0 | 真实1.0→虚拟0.8→真实0.6 | 3个 / 1220吨 | 20吨 |
| 双桥 | 0.4、0.8 | 真实0.8→虚拟0.6→虚拟0.5→真实0.4 | 4个 / 1240吨 | 40吨 |

两组均要求单链、无拆片、无禁止/欠重、自然结束、双审计通过，虚拟原型和谱系按实际契约精确检查。单桥日志必须记录`single/found=True`；双桥必须先`single/found=False`再`double/found=True`，均有真实`nopython=True`。每条记录绑定本请求，缺日志或全部回退不能通过。

源码测试通过真实TestClient执行求解，各场景同一应用两次，再新建应用一次；**新建应用不等于重启进程，不把这组数字用作冷启动性能测量**。新进程冷热成本仍依据阶段3/4；Windows脚本自身才会真正重启EXE。

### 2.2 Windows运行与资料保留

原正常可写目录的帮助/GET→POST→GET/重启/活动版本与运行库身份检查保留，之后另用程序目录外的临时YAML、数据库及诊断位置。正式策略完整保留，只改临时路径和回环监听；帮助30秒、启动60秒、普通HTTP10秒不变，求解HTTP等待为临时配置原总时限加既有HTTP余量。

权限检查使用`icacls`保存访问控制列表、对临时程序副本禁止写入/删除、验证目录和EXE实际拒绝写打开，再在`finally`恢复原权限。这里只改变脚本自己创建的临时副本，不操作正式发布目录。EXE子进程移除Python/Conda/Numba环境变量及外部PATH，保留系统目录。**这验证构建机上的隔离EXE子进程，不等于已在第二台没有Python的机器验证。**

Windows脚本保存帮助/服务/权限日志、清单、原规则回环结果、受控规则请求/响应、六次求解请求/响应及完整诊断。成功只清理大的临时程序副本，证据和临时运行库保留；失败也保留第一现场。通过控制台`evidence_directory`定位。不要将临时受控规则库替换现场库。

## 3. 本机检查记录

首次聚焦执行：78项通过、2项准备错误，13.35秒，退出1。两次实际求解均已成功且双审计通过；失败位于虚拟身份的组合断言，实际编号正确，错误是检查脚本把材料角色写为`generated_virtual`，既有源码枚举和真实响应均为`virtual_sphc`。仅修正检查及测试中的角色字面值，不改求解器或预期业务。首轮测试资料保留在系统pytest临时目录，不作为通过证据。

修正后发布聚焦80项通过，13.50秒，退出0；包含实际源码API单桥/双桥各3次成功求解、正式响应和真实数值日志核验。共享六目录累计3898项通过，61.56秒，退出0；Ruff与`git diff --check`退出0，10个目标文件UTF-8无BOM、所改文档相对链接通过，配置/数据库哈希均保持。精确暂存树最终验证结果见本项提交正文；不以首次失败记录代替最终测试。实际执行范围：

```bash
cd /Users/miles/dev/dev-py/APSGOV7
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/release/test_release_packaging.py tests/release/test_numeric_bridge_smoke.py -q
PYTHONDONTWRITEBYTECODE=1 /Users/miles/anaconda3/envs/aps_3.10.18/bin/python -m pytest -p no:cacheprovider tests/architecture tests/api tests/app tests/core tests/service tests/release -q
/Users/miles/anaconda3/bin/ruff check release/verify_release.py release/smoke_release.py tests/release/test_release_packaging.py tests/release/test_numeric_bridge_smoke.py
git diff --check
```

精确白名单暂存后使用`git write-tree`和`git archive`导出新临时目录，执行原残留工具和相同六目录累计测试；树身份、目录、数量、耗时和退出码记录在提交正文，避免文档自引用当前提交。V7共享目录旧V6残留缺失的已知边界保持，不重建旧残留。

保护对象核验口径：

| 文件 | 应保持的SHA-256 |
|---|---|
| `config/apsgo_v7_service.yaml` | `dc8f113ef85689fdc5ff2fecb0579f68c6892a8d8316c764568946bf341dd4c8` |
| `data/apsgo_v7_rules.sqlite3` | `8ed693ac2a1c2be5866c7b3ba8596eab58cbd9db6619a4be61c6c6af175b5288` |

## 4. Windows下一步

在Windows检出本项提交，先保持现场旧包、YAML和数据库原样，在源码仓库根目录执行：

```powershell
conda run -n aps_3.10.18 python -m pip install -r release/requirements-build.txt
.\release\build_exe.bat -CheckOnly
.\release\build_exe.bat
```

其他服务依赖按原项目说明准备。已有构建输出时先保留所需旧包，再按原约定使用`-Clean`；不得清理现场运行目录。检查失败保留完整第一条错误，不额外放宽规则/源码禁入或自动收集全部依赖。

通过后保留完整发布目录与清单、打印的烟测证据目录；再到目标Windows机器用明确版本的包、匹配输入/规则库和原预算做真实订单复测。主对照900/10/200000与现场310/10分开，未知版本旧包不可作为严格对照。首次/再次/重启、数值日志、完整耗时/CPU、两个拆单模式和双审计分别核验。此项不自动关闭前两专项的Windows边界或历史20对性能门，也不授权部署、停服、合并和推送。
