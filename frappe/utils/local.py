from contextvars import ContextVar
from typing import Any, TypeVar

_contextvar = ContextVar("frappe_local")

T = TypeVar("T")


class Local:
	"""
	For internal use only. Do not use this class directly.
	"""

	__slots__ = ()

	def __getattribute__(self, name: str) -> Any:
		# this is not needed as long as we have no other attributes than special methods
		# if name in _local_attributes:
		# 	return object.__getattribute__(self, name)

		obj = _contextvar.get(None)
		if obj is not None and name in obj:
			return obj[name]

		raise AttributeError(name)

	def __iter__(self):
		return iter((_contextvar.get({})).items())

	def __setattr__(self, name: str, value: Any) -> None:
		obj = _contextvar.get(None)
		if obj is None:
			obj = {}
			_contextvar.set(obj)

		obj[name] = value

	def __delattr__(self, name: str) -> None:
		obj = _contextvar.get(None)
		if obj is not None and name in obj:
			del obj[name]
			return

		raise AttributeError(name)

	def __call__(self, name: str) -> "LocalProxy":
		def _get_current_object() -> Any:
			obj = _contextvar.get(None)
			if obj is not None and name in obj:
				return obj[name]

			raise RuntimeError("object is not bound") from None

		lp = LocalProxy(_get_current_object)
		object.__setattr__(lp, "_get_current_object", _get_current_object)
		return lp


class LocalProxy[T]:
	"""Vendored werkzeug.local.LocalProxy (Phase 20): forwards everything to
	the object returned by `_get_current_object`. The storage is frappe's
	ContextVar dict above — only the proxy class was ever borrowed."""

	__slots__ = ("_get_current_object",)

	def __init__(self, get_current_object=None) -> None:
		if get_current_object is not None:
			object.__setattr__(self, "_get_current_object", get_current_object)

	# --- attribute / item forwarding ---------------------------------------

	def __getattr__(self, name: str) -> Any:
		return getattr(self._get_current_object(), name)

	def __setattr__(self, name: str, value: Any) -> None:
		setattr(self._get_current_object(), name, value)

	def __delattr__(self, name: str) -> None:
		delattr(self._get_current_object(), name)

	def __getitem__(self, key: str) -> Any:
		return self._get_current_object()[key]

	def __setitem__(self, key: str, value: Any) -> None:
		self._get_current_object()[key] = value

	def __delitem__(self, key: str) -> None:
		del self._get_current_object()[key]

	# --- common dunders werkzeug forwarded that frappe code relies on -------

	@property  # type: ignore[misc]
	def __class__(self):
		try:
			return type(self._get_current_object())
		except RuntimeError:
			return LocalProxy

	@property
	def __dict__(self):
		return self._get_current_object().__dict__

	def __bool__(self) -> bool:
		try:
			return bool(self._get_current_object())
		except RuntimeError:
			return False

	def __contains__(self, key: str) -> bool:
		return key in self._get_current_object()

	def __iter__(self):
		return iter(self._get_current_object())

	def __len__(self):
		return len(self._get_current_object())

	def __eq__(self, other) -> bool:
		try:
			return self._get_current_object() == other
		except RuntimeError:
			return NotImplemented

	def __ne__(self, other) -> bool:
		try:
			return self._get_current_object() != other
		except RuntimeError:
			return NotImplemented

	def __hash__(self):
		return hash(self._get_current_object())

	def __call__(self, *args, **kwargs):
		return self._get_current_object()(*args, **kwargs)

	def __str__(self) -> str:
		return str(self._get_current_object())

	def __repr__(self) -> str:
		try:
			return repr(self._get_current_object())
		except RuntimeError:
			return f"<{type(self).__name__} unbound>"

	def __dir__(self):
		try:
			return dir(self._get_current_object())
		except RuntimeError:
			return []

	def __instancecheck__(self, other):
		return isinstance(other, self._get_current_object())


def release_local(local):
	if isinstance(local, Local):
		_contextvar.set({})
		return

	raise TypeError(f"cannot release {local!r}")


# _local_attributes = frozenset(attr for attr in dir(Local))
