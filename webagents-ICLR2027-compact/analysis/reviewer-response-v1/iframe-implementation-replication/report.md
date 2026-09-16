# Independent iframe implementation replication

2 tasks x 2 origin axes x depths 0..3 x 2 independent implementations.

| Implementation | Reachable |
| --- | ---: |
| playwright-per-frame-aria-snapshot | 8/16 |
| selenium-recursive-page-source | 16/16 |

Selenium recursively switched into each iframe and inspected each frame's own page source. The second AX path used Playwright's public per-frame ARIA snapshot API; it does not call the core runner's raw `Accessibility.getFullAXTree` collector.
