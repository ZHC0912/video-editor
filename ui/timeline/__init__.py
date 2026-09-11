"""The timeline view.

A QGraphicsScene renders the project; a QGraphicsView shows it with a fixed
track header column beside it. Nothing in here writes to the model: gestures
are read, turned into commands, and applied by the window.

All tick/pixel arithmetic lives on :class:`ui.timeline.timeline_scene.TimelineScene`
and nowhere else.
"""
