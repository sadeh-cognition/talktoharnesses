"""Supervised process runtime for the split (copied from the former proxy runtime)."""

from tth_types.process import ProcessEvent as ProcessEvent
from tth_types.process import ProcessExitedEvent as ProcessExitedEvent
from tth_types.process import ProcessForcedTerminationEvent as ProcessForcedTerminationEvent
from tth_types.process import ProcessSilenceWarningEvent as ProcessSilenceWarningEvent
from tth_types.process import ProcessStartedEvent as ProcessStartedEvent
from tth_types.process import ProcessStderrTruncatedEvent as ProcessStderrTruncatedEvent

from tth_prime_agent.runtime.handle import STDERR_RETENTION_BYTES as STDERR_RETENTION_BYTES
from tth_prime_agent.runtime.handle import ProcessHandle as ProcessHandle
from tth_prime_agent.runtime.spec import ProcessSpec as ProcessSpec
from tth_prime_agent.runtime.supervisor import ProcessSupervisor as ProcessSupervisor
from tth_prime_agent.shared.policy import RuntimePolicy as RuntimePolicy
