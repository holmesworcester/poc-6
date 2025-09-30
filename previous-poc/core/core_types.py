"""
Protocol-agnostic base types for the core framework.

This module defines the fundamental types used by the framework without any
protocol-specific knowledge. Protocols extend these types with their own fields.
"""

from typing import TypedDict, Protocol, runtime_checkable, TypeVar, Any, Generic
from collections.abc import Callable


# The framework doesn't define envelope structure at all.
# Envelopes are just dicts that protocols define.
# The framework only passes them through handlers.


# The framework doesn't define delta structure either.
# Deltas are just dicts that the protocol's projectors return.
# The framework passes them to the database layer.


@runtime_checkable
class CommandFunc(Protocol):
    """Protocol for event creation commands"""
    def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Create an event envelope from parameters.

        The framework doesn't care about the structure,
        just that it returns a dict.
        """
        ...


@runtime_checkable
class ValidatorFunc(Protocol):
    """Protocol for event validators"""
    def __call__(self, envelope: dict[str, Any]) -> bool:
        """
        Validate an envelope.

        The framework only cares about the boolean result.
        """
        ...


@runtime_checkable
class ProjectorFunc(Protocol):
    """Protocol for event projectors"""
    def __call__(self, envelope: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Convert envelope to database deltas.

        The framework doesn't care about delta structure,
        just that it's a list of dicts.
        """
        ...


@runtime_checkable
class HandlerFunc(Protocol):
    """Protocol for pipeline handlers"""
    def __call__(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """
        Process envelope and return transformed envelope.

        Returns None if envelope should be dropped.
        """
        ...


@runtime_checkable
class FilterFunc(Protocol):
    """Protocol for handler filter functions"""
    def __call__(self, envelope: dict[str, Any]) -> bool:
        """
        Determine if handler should process this envelope.
        """
        ...


class HandlerDef(TypedDict):
    """Handler definition with filter and handler function"""
    name: str
    filter: FilterFunc
    handler: HandlerFunc


def command(_func: Callable[[dict[str, Any]], dict[str, Any] | list[dict[str, Any]]] | None = None, *,
            param_type: Any | None = None,
            result_type: Any | None = None) -> Callable[[Callable[[dict[str, Any]], dict[str, Any] | list[dict[str, Any]]]], Callable[[dict[str, Any]], dict[str, Any] | list[dict[str, Any]]]] | Callable[[dict[str, Any]], dict[str, Any] | list[dict[str, Any]]]:
    """
    Decorator to mark command functions.

    Usage:
      @command
      def create(...): ...

      @command(param_type=CreateParams, result_type=CreateResult)
      def create(...): ...

    The framework only validates that it takes and returns dicts.
    Protocol-specific validation is done by the protocol.
    """
    import functools
    import inspect

    def _decorate(func: Callable[[dict[str, Any]], dict[str, Any] | list[dict[str, Any]]]):
        sig = inspect.signature(func)
        params_list = list(sig.parameters.keys())

        if len(params_list) != 1:
            raise TypeError(
                f"{func.__name__} must have exactly one parameter, got {len(params_list)}"
            )

        @functools.wraps(func)
        def wrapper(params: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
            if not isinstance(params, dict):
                raise TypeError(f"Expected dict for params, got {type(params).__name__}")

            result = func(params)

            # Handle both single envelope and list of envelopes
            if not isinstance(result, (dict, list)):
                raise TypeError(f"{func.__name__} must return dict or list[dict], got {type(result).__name__}")

            if isinstance(result, list):
                for i, env in enumerate(result):
                    if not isinstance(env, dict):
                        raise TypeError(f"{func.__name__} envelope[{i}] must be dict, got {type(env).__name__}")

            return result

        # Marker attributes for discovery
        wrapper._is_command = True  # type: ignore
        wrapper._original_name = func.__name__  # type: ignore[attr-defined]
        # Optional type metadata for discovery
        if param_type is not None:
            wrapper._param_type = param_type  # type: ignore[attr-defined]
        if result_type is not None:
            wrapper._result_type = result_type  # type: ignore[attr-defined]

        return wrapper

    # Support bare @command and @command(...)
    if _func is None:
        return _decorate
    else:
        return _decorate(_func)


def validator(func: Callable[[dict[str, Any]], bool]) -> Callable[[dict[str, Any]], bool]:
    """
    Decorator to mark validator functions.

    The framework only validates the signature and return type.
    """
    import functools
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    if len(params) != 1:
        raise TypeError(
            f"{func.__name__} must have exactly one parameter, got {len(params)}"
        )

    @functools.wraps(func)
    def wrapper(envelope: dict[str, Any]) -> bool:
        if not isinstance(envelope, dict):
            raise TypeError(f"Expected dict for envelope, got {type(envelope).__name__}")

        result = func(envelope)

        if not isinstance(result, bool):
            raise TypeError(f"{func.__name__} must return bool, got {type(result).__name__}")

        return result

    wrapper._is_validator = True  # type: ignore
    return wrapper


def projector(func: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """
    Decorator to mark projector functions.

    The framework only validates that it returns a list of dicts.
    """
    import functools
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    if len(params) != 1:
        raise TypeError(
            f"{func.__name__} must have exactly one parameter, got {len(params)}"
        )

    @functools.wraps(func)
    def wrapper(envelope: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(envelope, dict):
            raise TypeError(f"Expected dict for envelope, got {type(envelope).__name__}")

        result = func(envelope)

        if not isinstance(result, list):
            raise TypeError(f"{func.__name__} must return list, got {type(result).__name__}")

        for i, delta in enumerate(result):
            if not isinstance(delta, dict):
                raise TypeError(f"{func.__name__} delta[{i}] must be dict, got {type(delta).__name__}")

        return result

    wrapper._is_projector = True  # type: ignore
    return wrapper


def handler(name: str, filter_func: FilterFunc) -> Callable[[Callable[[dict[str, Any]], dict[str, Any] | None]], HandlerDef]:
    """
    Decorator to mark handler functions.
    """
    def decorator(func: Callable[[dict[str, Any]], dict[str, Any] | None]) -> HandlerDef:
        return {
            "name": name,
            "filter": filter_func,
            "handler": func  # type: ignore
        }
    return decorator




from typing import Callable as _Callable


def response_handler(command_name: str) -> Any:
    # Deprecated shim
    def decorator(func: Any) -> Any:
        raise NotImplementedError("response handlers are deprecated; use flows instead")
    return decorator


def envelope_reducer(func: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """
    Decorator to mark envelope reducer functions.

    Reducers process a stream of envelopes and reduce them to summary data.
    Used by commands that generate many envelopes to avoid storing all of them.

    Takes:
    - accumulator: The current accumulated state (empty dict on first call)
    - envelope: The current envelope being processed

    Returns:
    - Updated accumulator
    """
    import functools
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    if len(params) != 2:
        raise TypeError(
            f"{func.__name__} must have exactly 2 parameters (accumulator, envelope), got {len(params)}"
        )

    @functools.wraps(func)
    def wrapper(accumulator: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(accumulator, dict):
            raise TypeError(f"Expected dict for accumulator, got {type(accumulator).__name__}")

        if not isinstance(envelope, dict):
            raise TypeError(f"Expected dict for envelope, got {type(envelope).__name__}")

        result = func(accumulator, envelope)

        if not isinstance(result, dict):
            raise TypeError(f"{func.__name__} must return dict, got {type(result).__name__}")

        return result

    wrapper._is_envelope_reducer = True  # type: ignore
    wrapper._is_envelope_reducer = True  # type: ignore
    return wrapper

def command_response(func: Callable[[str, list[dict[str, Any]], Any], dict[str, Any]]) -> Callable[[str, list[dict[str, Any]], Any], dict[str, Any]]:
    # Deprecated: response handlers removed in favor of flows
    raise NotImplementedError("response handlers are deprecated; use flows instead")
from typing import Callable as _Callable


def response_handler(command_name: str) -> Any:
    # Deprecated shim
    def decorator(func: Any) -> Any:
        raise NotImplementedError("response handlers are deprecated; use flows instead")
    return decorator


def envelope_reducer(func: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """
    Decorator to mark envelope reducer functions.

    Reducers process a stream of envelopes and reduce them to summary data.
    Used by commands that generate many envelopes to avoid storing all of them.

    Takes:
    - accumulator: The current accumulated state (empty dict on first call)
    - envelope: The current envelope being processed

    Returns:
    - Updated accumulator
    """
    import functools
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    if len(params) != 2:
        raise TypeError(
            f"{func.__name__} must have exactly 2 parameters (accumulator, envelope), got {len(params)}"
        )

    @functools.wraps(func)
    def wrapper(accumulator: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(accumulator, dict):
            raise TypeError(f"Expected dict for accumulator, got {type(accumulator).__name__}")

        if not isinstance(envelope, dict):
            raise TypeError(f"Expected dict for envelope, got {type(envelope).__name__}")

        result = func(accumulator, envelope)

        if not isinstance(result, dict):
            raise TypeError(f"{func.__name__} must return dict, got {type(result).__name__}")

        return result

    wrapper._is_envelope_reducer = True  # type: ignore
    return wrapper
