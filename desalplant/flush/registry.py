"""Registry of the active endpoint limits generation and its migrations."""

from .limits import EndpointLimits


class LimitRegistry:
    """Holds the active limits generation; migrating always stamps a new version.

    A migration is a new generation, not an in-place edit. Rounds already in
    flight keep the generation they were started with because the round stores a
    reference to the frozen :class:`EndpointLimits` it began under.
    """

    def __init__(self, current: EndpointLimits) -> None:
        self._current = current

    @property
    def current(self) -> EndpointLimits:
        """Generation that applies to rounds started right now."""
        return self._current

    def migrate(
        self,
        *,
        turbidity_limit: float,
        residual_limit: float,
        extend_seconds: int,
        max_seconds: int,
    ) -> EndpointLimits:
        """Adopt a new generation, stamping the next version number.

        The caller supplies the policy values; the registry is the single
        authority that advances the version.
        """
        migrated = EndpointLimits(
            version=self._current.version + 1,
            turbidity_limit=turbidity_limit,
            residual_limit=residual_limit,
            extend_seconds=extend_seconds,
            max_seconds=max_seconds,
        )
        self._current = migrated
        return migrated
