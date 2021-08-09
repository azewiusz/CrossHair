import dis

from crosshair.core import register_opcode_patch
from crosshair.libimpl.builtinslib import SymbolicStr
from crosshair.simplestructs import SimpleDict
from crosshair.tracers import TracingModule
from crosshair.tracers import frame_stack_read
from crosshair.tracers import frame_stack_write
from crosshair.util import debug

BINARY_SUBSCR = dis.opmap["BINARY_SUBSCR"]
COMPARE_OP = dis.opmap["COMPARE_OP"]
CONTAINS_OP = dis.opmap.get("CONTAINS_OP", 118)


def frame_op_arg(frame):
    return frame.f_code.co_code[frame.f_lasti + 1]


class DictionarySubscriptInterceptor(TracingModule):
    opcodes_wanted = frozenset([BINARY_SUBSCR])

    def trace_op(self, frame, codeobj, codenum):
        key = frame_stack_read(frame, -1)
        if isinstance(key, (int, float, str)):
            return
        container = frame_stack_read(frame, -2)
        if type(container) is not dict:
            return
        # SimpleDict won't hash the keys it's given!
        wrapped_dict = SimpleDict(list(container.items()))
        frame_stack_write(frame, -2, wrapped_dict)


_CONTAINMENT_OP_TYPES = tuple(
    i for (i, name) in enumerate(dis.cmp_op) if name in ("in", "not in")
)
assert len(_CONTAINMENT_OP_TYPES) in (0, 2)


class StringContainmentInterceptor(TracingModule):

    opcodes_wanted = frozenset(
        [
            COMPARE_OP,
            CONTAINS_OP,
        ]
    )

    def trace_op(self, frame, codeobj, codenum):
        if codenum == COMPARE_OP:
            compare_type = frame_op_arg(frame)
            if compare_type not in _CONTAINMENT_OP_TYPES:
                return
        container = frame_stack_read(frame, -1)
        item = frame_stack_read(frame, -2)
        if type(item) is SymbolicStr and type(container) is str:
            new_container = SymbolicStr(SymbolicStr._smt_promote_literal(container))
            frame_stack_write(frame, -1, new_container)


def make_registrations():
    register_opcode_patch(DictionarySubscriptInterceptor())
    register_opcode_patch(StringContainmentInterceptor())