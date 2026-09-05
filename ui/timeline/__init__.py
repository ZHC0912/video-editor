"""The timeline view.

A QGraphicsScene renders the project; a QGraphicsView shows it with a fixed
track header column beside it. Read only in this phase: it draws the model and
never changes it.

All tick/pixel arithmetic lives on :class:`ui.timeline.timeline_scene.TimelineScene`
and nowhere else.
"""
