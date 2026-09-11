# Project instructions

## Write failing tests before the fix

- For every bug fix or behavior change, first add or strengthen tests that pin
  down the failing or missing behavior. Do this before changing production code.
- Run those tests against the unfixed code and confirm that they fail for the
  expected behavioral reason. An import error, broken fixture, or unavailable
  dependency does not count as reproducing the bug.
- Only then implement the fix. The same regression tests must pass afterward;
  do not relax their assertions to accommodate an incorrect implementation.
- Run the relevant existing tests and appropriate QA to check for regressions.
  Report what failed before the fix and what passed afterward.

## Preserve regression coverage

- Assume existing tests represent real regressions, even when their original
  motivation is unclear or their behavior seems unusual.
- When a test fails, investigate the behavior and history before deciding the
  test is wrong. Do not delete, skip, disable, or weaken it merely to make the
  suite green.
- Remove a test only when there is strong evidence and high confidence that its
  coverage is no longer needed, such as intentionally removed behavior or
  equivalent coverage retained elsewhere. Explain that evidence and any
  replacement coverage in the change description.
- If there is doubt, keep the test.
