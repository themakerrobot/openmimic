"""OpenMimic MuJoCo 가상 장치 — 로봇 모델 + 과제 장면(책상·물체·목표)·카메라·자동 시연·성공 판정.

tools_simarms.py 가 가상 팔로워의 '몸' 으로 씁니다. 서보 프로토콜(가상 시리얼 sim://)은 그대로 두고,
팔로워 서보의 실제 위치(엔코더)를 MuJoCo 관절각으로, 목표 위치를 위치 액추에이터 명령으로 연결합니다.
카메라(top · wrist)는 이 프로세스가 렌더링해 공유 메모리 파일에 쓰고, openmimic_simcam 이 읽습니다.

  관절각(rad) = (엔코더 틱 - 가운데 틱) × 2π / 한 바퀴 틱      (STS3215: 4096 틱, 가운데 2048)

확장 — 로봇과 과제를 따로 둡니다.
  ROBOTS[이름]  모델 파일(MJCF), 관절 이름, 그리퍼·끝점(site), 열림/닫힘 각도, 시작 자세, 손목 카메라 위치, 서보 틱 환산.
                URDF 만 있으면 MuJoCo 가 그대로 읽습니다 (메시 경로가 맞아야 함). 위치 액추에이터가 없으면 붙여야 합니다.
  TASKS[이름]   장면 만들기(build) · 물체 다시 놓기(reset) · 시연 궤적(plan) · 성공 판정(judge).
                새 과제는 Task 를 상속해 이 네 가지를 채우면 수집(자동 시연)·실행(자동 판정) 화면이 그대로 씁니다.

기본 과제(cube_to_target): 책상 위 빨간 큐브를 집어 초록 칸에 놓기. 리더는 IK 로 만든 시연 궤적을 따라 움직이고,
큐브가 초록 칸 안에 놓이고 그리퍼가 놓아 주면 성공입니다.

그립 보조: SO-101 턱의 충돌 모양은 메시의 볼록 껍질이라 마찰만으로는 큐브가 밀려나며 미끄러집니다.
그래서 그리퍼가 닫히는 중이고 물체가 끝점 근처에 있으면 물체를 그리퍼에 붙이고, 그리퍼가 열리면 놓습니다.
정책은 여전히 물체 앞에서 그리퍼를 닫는 동작을 배워야 합니다.

모델: urdf/so101_new_calib.xml (TheRobotStudio SO-ARM100, Apache 2.0). 책상·물체·카메라는 불러올 때 붙입니다.
"""
import math
import threading
from pathlib import Path

import numpy as np

import openmimic_platform as P

HERE = Path(__file__).resolve().parent
FRAME_DIR = P.frame_dir()
FRAME_MAGIC = 0x4F4D5331

# ============================================================================ 로봇
ROBOTS = {
    "so101": {
        "label": "SO-ARM101",
        "mjcf": HERE / "urdf" / "so101_new_calib.xml",
        "joints": ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"),
        "tcp_site": "gripperframe",         # 그리퍼 끝점
        "approach_axis": 0,                 # 끝점 site 의 x 축 = 그리퍼가 향하는 방향
        "grip_body": "gripper",
        "contact_bodies": ("gripper", "moving_jaw_so101_v1"),    # 물체와 닿는 몸체 (나머지는 충돌 끔 — 계산량 절약)
        "grip_open": 0.9, "grip_closed": -0.15,                  # 그리퍼 관절각 (rad)
        "home": (0.0, -1.2, 1.1, 0.9, 0.0, -0.15),
        "ticks_per_rev": 4096, "center_tick": 2048,              # STS3215
        "wrist_cam": {"body": "gripper", "pos": [0.0, 0.085, -0.02], "xyaxes": [-1, 0, 0, 0, -0.45, 0.89], "fovy": 75},
        "top_cam": {"pos": [0.62, 0.0, 0.42], "target": [0.16, 0.0, 0.0], "fovy": 50},
    },
}


# ============================================================================ 과제
class Task:
    """과제 하나 — 상속해 build · bind · reset · plan · judge 를 채웁니다."""
    name = ""
    label = ""
    demo_s = 14.0                         # 자동 시연 길이 (s) — 수집 화면의 에피소드 길이 기본값에 씀

    def build(self, spec, robot):
        """MjSpec 에 책상·물체·목표를 붙입니다."""

    def bind(self, world):
        """컴파일된 모델에서 물체 id 등을 찾아 둡니다."""

    def reset(self, world, rng):
        """물체를 새 위치에 놓습니다 (world.lock 안에서 불림)."""

    def plan(self, world, start_q):
        """[(시각 s, 관절각)] — 리더가 따라 움직일 시연 경유점."""
        return [(0.0, start_q)]

    def step(self, world):
        """물리 한 스텝마다 (그립 보조 등)."""

    def judge(self, world):
        """성공이면 True."""
        return False

    def state(self, world):
        return {}


class CubeToTarget(Task):
    name = "cube_to_target"
    label = "큐브를 집어 초록 칸에 놓기"
    demo_s = 14.0
    CUBE_HALF = 0.0125                    # 2.5 cm 큐브
    TARGET = (0.20, -0.13)                # 초록 칸 중심 (m, 로봇 기준: x 앞, y 왼쪽)
    TARGET_HALF = 0.045
    CUBE_X, CUBE_Y = (0.17, 0.25), (0.03, 0.14)    # 큐브가 놓이는 범위
    GRASP_Q = 0.55                        # 그리퍼가 이보다 닫히면 '쥐는 중' (rad)
    GRASP_DIST = 0.03                     # 큐브 중심이 끝점에서 이 안이면 붙임 (m)

    def build(self, spec, robot):
        import mujoco
        wb = spec.worldbody
        spec.add_material(name="table_mat", rgba=[0.55, 0.5, 0.45, 1])
        spec.add_material(name="target_mat", rgba=[0.2, 0.75, 0.3, 1])
        spec.add_material(name="cube_mat", rgba=[0.85, 0.15, 0.12, 1])
        wb.add_geom(name="table", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[0.6, 0.6, 0.01], material="table_mat",
                    contype=1, conaffinity=1, friction=[1.0, 0.05, 0.001])
        wb.add_geom(name="target", type=mujoco.mjtGeom.mjGEOM_BOX, pos=[self.TARGET[0], self.TARGET[1], 0.0005],
                    size=[self.TARGET_HALF, self.TARGET_HALF, 0.0005], material="target_mat", contype=0, conaffinity=0)
        cube = wb.add_body(name="cube", pos=[0.2, 0.08, self.CUBE_HALF])
        cube.add_freejoint(name="cube_free")
        cube.add_geom(name="cube", type=mujoco.mjtGeom.mjGEOM_BOX, size=[self.CUBE_HALF] * 3, mass=0.02,
                      material="cube_mat", contype=1, conaffinity=3, condim=4, friction=[1.5, 0.05, 0.001])

    def bind(self, world):
        m = world.model
        self.qa = m.joint("cube_free").qposadr[0]
        self.da = m.joint("cube_free").dofadr[0]
        self.geom = m.geom("cube").id
        self.col = (int(m.geom_contype[self.geom]), int(m.geom_conaffinity[self.geom]))
        self.attached = None              # 붙어 있으면 그리퍼 좌표계에서의 큐브 자세 (pos, quat)

    def pos(self, world):
        return world.data.qpos[self.qa:self.qa + 3].copy()

    def reset(self, world, rng):
        d, m = world.data, world.model
        x, y = rng.uniform(*self.CUBE_X), rng.uniform(*self.CUBE_Y)
        yaw = math.atan2(y, x) + rng.uniform(-0.25, 0.25)
        d.qpos[self.qa:self.qa + 3] = [x, y, self.CUBE_HALF + 0.001]
        d.qpos[self.qa + 3:self.qa + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
        d.qvel[self.da:self.da + 6] = 0
        self.attached = None
        m.geom_contype[self.geom], m.geom_conaffinity[self.geom] = self.col

    def step(self, world):
        """그립 보조 — 닫는 중 + 끝점 근처면 붙이고, 열리면 놓습니다."""
        mj, m, d = world.mj, world.model, world.data
        gq = d.qpos[world.qadr[-1]]
        gp, gm = d.xpos[world.grip_body], d.xmat[world.grip_body].reshape(3, 3)
        a = self.qa
        gquat = np.zeros(4)
        mj.mju_mat2Quat(gquat, gm.reshape(-1))
        if self.attached is None:
            closing = world.hold_ctrl[-1] < gq
            if gq < self.GRASP_Q and closing and np.linalg.norm(d.qpos[a:a + 3] - d.site_xpos[world.site]) < self.GRASP_DIST:
                inv = np.zeros(4)
                mj.mju_negQuat(inv, gquat)
                rel_q = np.zeros(4)
                mj.mju_mulQuat(rel_q, inv, d.qpos[a + 3:a + 7])
                self.attached = (gm.T @ (d.qpos[a:a + 3] - gp), rel_q)
                m.geom_contype[self.geom] = m.geom_conaffinity[self.geom] = 0
        elif gq > self.GRASP_Q + 0.1:
            self.attached = None
            m.geom_contype[self.geom], m.geom_conaffinity[self.geom] = self.col
        if self.attached is not None:
            rel_p, rel_q = self.attached
            q = np.zeros(4)
            mj.mju_mulQuat(q, gquat, rel_q)
            d.qpos[a:a + 3] = gp + gm @ rel_p
            d.qpos[a + 3:a + 7] = q
            d.qvel[self.da:self.da + 6] = 0

    def judge(self, world):
        c = self.pos(world)
        tip = world.data.site_xpos[world.site]
        tx, ty = self.TARGET
        inside = abs(c[0] - tx) < self.TARGET_HALF and abs(c[1] - ty) < self.TARGET_HALF
        resting = c[2] < self.CUBE_HALF + 0.006
        released = np.linalg.norm(tip - c) > 0.035 or world.data.qpos[world.qadr[-1]] > 0.4
        return bool(inside and resting and released)

    def state(self, world):
        return {"cube": [round(float(v), 3) for v in self.pos(world)]}

    def plan(self, world, start_q):
        r = world.robot
        d = world.data
        c = self.pos(world)
        quat = d.qpos[self.qa + 3:self.qa + 7].copy()
        yaw = 2 * math.atan2(quat[3], quat[0])
        home = np.array(r["home"], float)
        g_open, g_closed = r["grip_open"], r["grip_closed"]

        def pose(x, y, z, grip):
            q = world.ik([x, y, z], home)
            # 턱이 큐브 면과 나란하도록 wrist_roll 을 큐브 방향(팔 방향 기준)에 맞춤 — 90° 대칭이라 가까운 쪽
            rel = (yaw - math.atan2(y, x)) % (math.pi / 2)
            if rel > math.pi / 4:
                rel -= math.pi / 2
            q[-2] = np.clip(q[-2] + rel, *world.jrange[-2])
            q[-1] = grip
            return q

        tx, ty = self.TARGET
        above, low = 0.09, self.CUBE_HALF + 0.002
        return [
            (0.0, np.asarray(start_q, float)),
            (1.5, home),
            (3.5, pose(c[0], c[1], above, g_open)),
            (5.0, pose(c[0], c[1], low, g_open)),
            (6.0, pose(c[0], c[1], low, g_closed)),
            (7.2, pose(c[0], c[1], above, g_closed)),
            (9.4, pose(tx, ty, above, g_closed)),
            (10.6, pose(tx, ty, low + 0.004, g_closed)),
            (11.4, pose(tx, ty, low + 0.004, g_open)),
            (12.4, pose(tx, ty, above, g_open)),
            (14.0, home),
        ]


TASKS = {CubeToTarget.name: CubeToTarget}


# ============================================================================ 공통
def tick_from_rad(q, robot="so101"):
    r = ROBOTS[robot]
    return r["center_tick"] + np.asarray(q, float) * r["ticks_per_rev"] / (2 * math.pi)


def rad_from_tick(t, robot="so101"):
    r = ROBOTS[robot]
    return (np.asarray(t, float) - r["center_tick"]) * (2 * math.pi) / r["ticks_per_rev"]


def available(robot="so101"):
    """mujoco 가 설치돼 있는지 — import 하지 않고 확인합니다 (import 하면 렌더 백엔드가 그때 정해짐)."""
    import importlib.util
    return importlib.util.find_spec("mujoco") is not None and robot in ROBOTS and Path(ROBOTS[robot]["mjcf"]).is_file()


def gl_ready():
    """렌더 백엔드(MUJOCO_GL)를 정하고 렌더를 쓸 수 있으면 True — openmimic_platform.setup_mujoco_gl.
    MuJoCo 는 import 할 때 백엔드를 정하므로 World 를 만들기 전에 부릅니다."""
    return P.setup_mujoco_gl()


def _look_at(pos, target, up=(0, 0, 1)):
    """MuJoCo 카메라 xyaxes — 카메라는 자기 -z 방향을 봅니다."""
    pos, target, up = map(np.asarray, (pos, target, up))
    f = target - pos
    f = f / np.linalg.norm(f)
    x = np.cross(f, up)
    x = x / np.linalg.norm(x)
    y = np.cross(x, f)
    return np.concatenate([x, y]).tolist()


def build_model(robot, task):
    import mujoco
    r = ROBOTS[robot]
    spec = mujoco.MjSpec.from_file(str(r["mjcf"]))
    spec.option.timestep = 0.002
    for g in spec.geoms:
        if g.classname.name == "collision":
            body = g.parent.name if getattr(g, "parent", None) else ""
            if body in r["contact_bodies"]:
                g.contype, g.conaffinity = 2, 1
                g.friction = [1.5, 0.05, 0.001]
            else:
                g.contype, g.conaffinity = 0, 0
    wb = spec.worldbody
    wb.add_light(pos=[0.3, 0, 1.2], dir=[0, 0, -1], diffuse=[0.8, 0.8, 0.8])
    wb.add_light(pos=[-0.4, 0.4, 0.8], dir=[0.5, -0.5, -0.6], diffuse=[0.35, 0.35, 0.35])
    task.build(spec, r)
    tc = r["top_cam"]
    wb.add_camera(name="top", pos=tc["pos"], xyaxes=_look_at(tc["pos"], tc["target"]), fovy=tc["fovy"])
    wc = r["wrist_cam"]
    body = next(b for b in spec.bodies if b.name == wc["body"])
    body.add_camera(name="wrist", pos=wc["pos"], xyaxes=wc["xyaxes"], fovy=wc["fovy"])
    return spec.compile()


class World:
    """MuJoCo 장면 하나 — 물리 스텝·카메라 렌더·시연 궤적·성공 판정. 모든 접근은 self.lock 으로."""

    def __init__(self, robot="so101", task="cube_to_target", cam_size=(320, 240), cams=("top", "wrist"), seed=None):
        if not gl_ready():
            raise RuntimeError("MuJoCo 렌더 백엔드를 쓸 수 없습니다 (MUJOCO_GL)")
        import mujoco
        self.mj = mujoco
        self.robot_name = robot
        self.robot = ROBOTS[robot]
        self.task = TASKS[task]()
        self.model = build_model(robot, self.task)
        self.data = mujoco.MjData(self.model)
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(seed)
        m = self.model
        joints = self.robot["joints"]
        self.qadr = np.array([m.joint(j).qposadr[0] for j in joints])
        self.dadr = np.array([m.joint(j).dofadr[0] for j in joints])
        self.act = np.array([m.actuator(j).id for j in joints])
        self.jrange = np.array([m.joint(j).range for j in joints])
        self.site = m.site(self.robot["tcp_site"]).id
        self.grip_body = m.body(self.robot["grip_body"]).id
        self.hold_ctrl = np.zeros(len(joints))
        self.cam_size = cam_size
        self.cams = list(cams)
        self.success = False
        self.n_reset = 0
        self.task.bind(self)
        mujoco.mj_forward(m, self.data)
        self.reset_scene()
        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        self._renderer = None
        self._frames = {}

    # ---------------------------------------------------------------- 관절 ↔ 서보 틱
    def ticks(self, q):
        return tick_from_rad(q, self.robot_name)

    def rad(self, t):
        return rad_from_tick(t, self.robot_name)

    def ranges_ticks(self, margin=0.03):
        """서보 캘리브레이션 범위로 쓸 (lo, hi) 틱 — 관절 범위보다 조금 안쪽."""
        lo, hi = self.jrange[:, 0], self.jrange[:, 1]
        span = hi - lo
        return self.ticks(lo + span * margin), self.ticks(hi - span * margin)

    def joint_ticks(self):
        with self.lock:
            return self.ticks(self.data.qpos[self.qadr].copy())

    def set_targets(self, ticks, torque):
        """팔로워 서보 상태 → 액추에이터. 토크 꺼진 관절은 지금 자세를 유지합니다 (중력으로 떨어지지 않게)."""
        with self.lock:
            q = self.data.qpos[self.qadr]
            for i in range(len(self.qadr)):
                if torque[i] and ticks[i] is not None:
                    self.hold_ctrl[i] = np.clip(self.rad(ticks[i]), *self.jrange[i])
                elif not torque[i]:
                    self.hold_ctrl[i] = q[i]

    def teleport(self, ticks):
        """토크가 꺼진 팔로워를 '손으로' 움직인 것처럼 관절각을 바로 놓습니다 (마법사 쓸기용)."""
        with self.lock:
            q = np.clip(self.rad(ticks), self.jrange[:, 0], self.jrange[:, 1])
            self.data.qpos[self.qadr] = q
            self.data.qvel[self.dadr] = 0
            self.hold_ctrl[:] = q
            self.mj.mj_forward(self.model, self.data)

    def step(self, dt):
        with self.lock:
            self.data.ctrl[self.act] = self.hold_ctrl
            n = max(1, int(round(dt / self.model.opt.timestep)))
            for _ in range(min(n, 50)):
                self.mj.mj_step(self.model, self.data)
                self.task.step(self)
            self.success = self.task.judge(self)

    # ---------------------------------------------------------------- 장면
    def reset_scene(self):
        """물체를 무작위 위치에 다시 놓습니다 (팔은 그대로)."""
        with self.lock:
            self.task.reset(self, self.rng)
            self.mj.mj_forward(self.model, self.data)
            self.success = False
            self.n_reset += 1

    def state(self):
        with self.lock:
            return dict(self.task.state(self), success=self.success, resets=self.n_reset,
                        task=self.task.name, label=self.task.label)

    def plan_demo(self, start_q=None):
        with self.lock:
            q_now = self.data.qpos[self.qadr].copy() if start_q is None else np.asarray(start_q, float)
            return self.task.plan(self, q_now)

    # ---------------------------------------------------------------- IK
    def ik(self, pos, q0, iters=200):
        """그리퍼 끝점을 pos 로, 그리퍼를 아래(-z)로 향하게 하는 관절각 — 그리퍼 관절은 그대로."""
        mj, m = self.mj, self.model
        d = mj.MjData(m)
        q = np.array(q0, float)
        jac_p = np.zeros((3, m.nv))
        jac_r = np.zeros((3, m.nv))
        cols = self.dadr[:-1]
        ax = self.robot["approach_axis"]
        for _ in range(iters):
            d.qpos[self.qadr] = q
            mj.mj_kinematics(m, d)
            mj.mj_comPos(m, d)
            err_p = np.asarray(pos) - d.site_xpos[self.site]
            app = d.site_xmat[self.site].reshape(3, 3)[:, ax]
            err_r = np.cross(app, np.array([0, 0, -1.0]))
            if np.linalg.norm(err_p) < 1e-4 and np.linalg.norm(err_r) < 1e-3:
                break
            mj.mj_jacSite(m, d, jac_p, jac_r, self.site)
            J = np.vstack([jac_p[:, cols], 0.3 * jac_r[:, cols]])
            e = np.concatenate([err_p, 0.3 * err_r])
            dq = J.T @ np.linalg.solve(J @ J.T + 1e-4 * np.eye(6), e)
            q[:-1] += np.clip(dq, -0.2, 0.2)
            q[:-1] = np.clip(q[:-1], self.jrange[:-1, 0], self.jrange[:-1, 1])
        return q

    # ---------------------------------------------------------------- 렌더 → 공유 프레임
    def render_all(self):
        """카메라마다 한 장씩 렌더해 FRAME_DIR/cam_<이름>.rgb 에 씁니다 (머리 16바이트: magic,w,h,seq)."""
        w, h = self.cam_size
        if self._renderer is None:
            self._renderer = self.mj.Renderer(self.model, h, w)
        for name in self.cams:
            with self.lock:
                self._renderer.update_scene(self.data, camera=name)
            self._write_frame(name, self._renderer.render())

    def _write_frame(self, name, img):
        h, w = img.shape[:2]
        f = self._frames.get(name)
        path = FRAME_DIR / f"cam_{name}.rgb"
        if f is None or f["shape"] != (h, w):
            size = 16 + h * w * 3
            # TODO(win-verify): 쓰는 쪽·읽는 쪽 memmap 동시 사용
            # 크기가 같은 파일이 이미 있으면 그대로 다시 씁니다 — Windows 는 다른 프로세스(가상 카메라)가
            # 매핑 중인 파일을 잘라내거나 지울 수 없습니다 (Linux 는 시작 때 옛 프레임을 지워 두므로 항상 새 파일)
            if not (path.is_file() and path.stat().st_size == size):
                with open(path, "wb") as fh:
                    fh.truncate(size)
            mm = np.memmap(path, dtype=np.uint8, mode="r+", shape=(size,))
            mm[:16] = np.frombuffer(np.array([FRAME_MAGIC, w, h, 0], dtype=np.uint32).tobytes(), np.uint8)
            f = self._frames[name] = {"mm": mm, "shape": (h, w), "seq": 0}
        mm = f["mm"]
        mm[16:] = img.reshape(-1)
        f["seq"] += 1
        mm[12:16] = np.frombuffer(np.uint32(f["seq"]).tobytes(), np.uint8)

    def close(self):
        for name in self.cams:
            try:
                (FRAME_DIR / f"cam_{name}.rgb").unlink()
            except OSError:
                pass


def interp(pts, t):
    """경유점 사이를 부드럽게 (smoothstep)."""
    if t <= pts[0][0]:
        return np.asarray(pts[0][1])
    for (t0, q0), (t1, q1) in zip(pts, pts[1:]):
        if t <= t1:
            s = (t - t0) / max(1e-6, t1 - t0)
            s = s * s * (3 - 2 * s)
            return np.asarray(q0) + (np.asarray(q1) - np.asarray(q0)) * s
    return np.asarray(pts[-1][1])
