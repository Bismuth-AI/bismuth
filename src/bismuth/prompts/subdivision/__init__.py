"""Asking what has grown here, and who belongs to it.

The shape of each question is the point, and the shape is not "divide this".

**Normal growth is not a partition.** A partition has to account for every document, and
a heterogeneous pile cannot honestly be accounted for without inventing a remainder
class. Forbidding remainder names does not work, because the partition demands one.

So instead one class is drawn out at a time (docs/spec/subdivision.md 2). *Emerging*
names a single class and nothing else; *Members* says who belongs to that one class.
Neither reply has a slot the leftovers could go in, and the leftovers stay in the folder
they are already in, which is what SPEC.md 3.4 says should happen to them.

Asking this repeatedly is safe in a way that asking "how would you divide this" is not:
it can add one sibling or route a loose document behind an existing sign, but it cannot
change the axis or redraw existing siblings.

The modules follow the operators of ADR-0018: :mod:`emerging` draws a class out or routes
a document behind one that stands, :mod:`reshaping` stands folders together or dissolves
a level, :mod:`checks` holds a proposal to the two closed questions, and :mod:`shared`
carries what a folder name has to do in all of them.
"""

from bismuth.prompts.subdivision.checks import build_axis_check, build_name_check
from bismuth.prompts.subdivision.emerging import (
    build_axis,
    build_class_name,
    build_class_sign,
    build_emerging,
    build_existing_assignments,
    build_existing_choice,
    build_group,
    build_member_choice,
    build_members,
)
from bismuth.prompts.subdivision.reshaping import (
    build_covers_check,
    build_grouping,
    build_grouping_member,
    build_shelf_check,
    build_split_check,
)
from bismuth.prompts.subdivision.shared import (
    Axis,
    ClassName,
    ClassSign,
    Division,
    Emerging,
    ExistingAssignment,
    ExistingAssignments,
    Gathered,
    Group,
    Grouping,
    Members,
    in_their_language,
)

__all__ = [
    "Axis",
    "ClassName",
    "ClassSign",
    "Division",
    "Emerging",
    "ExistingAssignment",
    "ExistingAssignments",
    "Gathered",
    "Group",
    "Grouping",
    "Members",
    "build_axis",
    "build_axis_check",
    "build_class_name",
    "build_class_sign",
    "build_covers_check",
    "build_emerging",
    "build_existing_assignments",
    "build_existing_choice",
    "build_group",
    "build_grouping",
    "build_grouping_member",
    "build_member_choice",
    "build_members",
    "build_name_check",
    "build_shelf_check",
    "build_split_check",
    "in_their_language",
]
