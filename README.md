# GSLPython

`GSLPython` is import-activated.

Add this at the top of a Python module:

```python
import GSLPython
```

When imported, `GSLPython` automatically detects the importer module and applies best-effort runtime acceleration wrappers to user-defined functions and class methods while keeping the same namespace.
