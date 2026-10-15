"""ACT2 — LeRobot policy plugin.

Registers the policy type ``act2`` (``--policy.type=act2``). Importing this package is enough:
lerobot discovers installed packages whose name starts with ``lerobot_policy_`` and imports them.
"""

from .configuration_act2 import ACT2Config
from .modeling_act2 import ACT2Policy
from .processor_act2 import make_act2_pre_post_processors

__all__ = ["ACT2Config", "ACT2Policy", "make_act2_pre_post_processors"]
