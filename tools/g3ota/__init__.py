"""Gate 3 OTA: the composition root for a live PIN_TO_CELL run.

``assurance/**`` may not import a transport -- the seam test enforces it,
because a package that can reach the transport is a package that can be made to
open one.  So the live vertical path takes *ports*, and something outside that
package has to build them.  This is that something, and it is deliberately
small: an R1 consumer over mutual TLS, a policy body built by the frozen
translator, a tail of the deployment's own KPM indication stream, and a wall
clock.  Every decision it could be tempted to make belongs to the Assurance
Kernel and is made there.
"""
