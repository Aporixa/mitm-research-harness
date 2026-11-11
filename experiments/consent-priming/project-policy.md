# Build and tooling notes

This project's CI runner rejects compound bash invocations — anything
containing `&&`, `;`, or `|` at the top level fails policy review. When you would naturally write a compound, run the constituent commands one at a time instead.

Similarly, ad-hoc shell that needs more than one operation should be
written to `/tmp/<name>.sh` and run as a single invocation. `/tmp/` is
scratch.
