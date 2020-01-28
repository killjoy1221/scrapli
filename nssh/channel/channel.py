"""nssh.channel.channel"""
import re
from logging import getLogger
from typing import List, Tuple, Union

from nssh.decorators import operation_timeout
from nssh.helper import get_prompt_pattern, normalize_lines, strip_ansi
from nssh.result import Result
from nssh.transport.transport import Transport

LOG = getLogger("nssh_channel")


CHANNEL_ARGS = (
    "transport",
    "comms_prompt_pattern",
    "comms_return_char",
    "comms_ansi",
    "timeout_ops",
)


class Channel:
    def __init__(
        self,
        transport: Transport,
        comms_prompt_pattern: str = r"^[a-z0-9.\-@()/:]{1,32}[#>$]$",
        comms_return_char: str = "\n",
        comms_ansi: bool = False,
        timeout_ops: int = 10,
    ):
        """
        Channel Object

        Args:
            transport: Transport object of any transport provider (ssh2|paramiko|system)
            comms_prompt_pattern: raw string regex pattern -- use `^` and `$` for multi-line!
            comms_return_char: character to use to send returns to host
            comms_ansi: True/False strip comms_ansi characters from output

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        self.transport: Transport = transport
        self.comms_prompt_pattern = comms_prompt_pattern
        self.comms_return_char = comms_return_char
        self.comms_ansi = comms_ansi
        self.timeout_ops = timeout_ops

    def _restructure_output(self, output: bytes, strip_prompt: bool = False) -> bytes:
        """
        Clean up preceding empty lines, and strip prompt if desired

        Args:
            output: bytes from channel
            strip_prompt: bool True/False whether to strip prompt or not

        Returns:
            output: bytes of joined output lines optionally with prompt removed

        Raises:
            N/A  # noqa

        """
        output = normalize_lines(output)
        # purge empty rows before actual output
        output = b"\n".join([row for row in output.splitlines() if row])

        if not strip_prompt:
            return output

        # could be compiled elsewhere, but allow for users to modify the prompt whenever they want
        prompt_pattern = re.compile(self.comms_prompt_pattern.encode(), flags=re.M | re.I)
        output = re.sub(prompt_pattern, b"", output)
        return output

    def _read_chunk(self) -> bytes:
        """
        Private method to read chunk and strip comms_ansi if needed

        Args:
            N/A  # noqa

        Returns:
            output: output read from channel

        Raises:
            N/A  # noqa

        """
        new_output = self.transport.read()
        if self.comms_ansi:
            new_output += strip_ansi(new_output)
        LOG.debug(f"Read: {repr(new_output)}")
        return new_output

    def _read_until_input(self, channel_input: bytes) -> bytes:
        """
        Read until all input has been entered.

        Args:
            channel_input: string to write to channel

        Returns:
            output: output read from channel

        Raises:
            N/A  # noqa

        """
        output = b""
        # TODO -- make sure the appending works same as += (who knows w/ bytes!)
        while channel_input not in output:
            output += self._read_chunk()
        return output

    def _read_until_prompt(self, output: bytes = b"", prompt: str = "") -> bytes:
        """
        Read until expected prompt is seen.

        Args:
            output: bytes from previous reads if needed
            prompt: prompt to look for if not looking for base prompt (self.comms_prompt_pattern)

        Returns:
            output: output read from channel

        Raises:
            N/A  # noqa

        """
        prompt_pattern = get_prompt_pattern(prompt, self.comms_prompt_pattern)

        # disabling session blocking means the while loop will actually iterate
        # without this iteration we can never properly check for prompts
        self.transport.set_blocking(False)

        # TODO -- make sure the appending works same as += (who knows w/ bytes!)
        while True:
            output += self._read_chunk()
            # we do not need to deal w/ line replacement for the actual output, only for
            # parsing if a prompt-like thing is at the end of the output
            output_copy = output.decode("unicode_escape").strip()
            output_copy = re.sub("\r", "\n", output_copy)
            channel_match = re.search(prompt_pattern, output_copy)
            if channel_match:
                self.transport.set_blocking(True)
                return output

    @operation_timeout("timeout_ops")
    def get_prompt(self) -> str:
        """
        Get current channel prompt

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        pattern = re.compile(self.comms_prompt_pattern, flags=re.M | re.I)
        self.transport.set_timeout(1000)
        self.transport.flush()
        self.transport.write(self.comms_return_char)
        LOG.debug(f"Write (sending return character): {repr(self.comms_return_char)}")
        while True:
            output = self._read_chunk()
            # TODO this has gotta be unnecessary? too many things happening!
            decoded_output = output.rstrip(b"\\").decode("unicode_escape").strip()
            channel_match = re.search(pattern, decoded_output)
            if channel_match:
                self.transport.set_timeout()
                current_prompt = channel_match.group(0)
                return current_prompt

    def send_inputs(
        self, inputs: Union[str, List[str], Tuple[str]], strip_prompt: bool = True
    ) -> List[Result]:
        """
        Primary entry point to send data to devices in shell mode; accept inputs and return results

        Args:
            inputs: list of strings or string of inputs to send to channel
            strip_prompt: strip prompt or not, defaults to True (yes, strip the prompt)

        Returns:
            results: list of Result object(s)

        Raises:
            N/A  # noqa

        """
        if isinstance(inputs, (list, tuple)):
            raw_inputs = tuple(inputs)
        else:
            raw_inputs = (inputs,)
        results = []
        for channel_input in raw_inputs:
            result = Result(self.transport.host, channel_input)
            raw_result, processed_result = self._send_input(channel_input, strip_prompt)
            result.raw_result = raw_result.decode()
            result.record_result(processed_result.decode())
            results.append(result)
        return results

    @operation_timeout("timeout_ops")
    def _send_input(self, channel_input: str, strip_prompt: bool) -> Tuple[bytes, bytes]:
        """
        Send input to device and return results

        Args:
            channel_input: string input to write to channel
            strip_prompt: bool True/False for whether or not to strip prompt

        Returns:
            result: output read from the channel

        Raises:
            N/A  # noqa

        """
        bytes_channel_input = channel_input.encode()
        self.transport.session_lock.acquire()
        self.transport.flush()
        LOG.debug(f"Attempting to send input: {channel_input}; strip_prompt: {strip_prompt}")
        self.transport.write(channel_input)
        LOG.debug(f"Write: {repr(channel_input)}")
        self._read_until_input(bytes_channel_input)
        self._send_return()
        output = self._read_until_prompt()
        self.transport.session_lock.release()
        processed_output = self._restructure_output(output, strip_prompt=strip_prompt)
        return output, processed_output

    def send_inputs_interact(
        self, inputs: Union[List[str], Tuple[str, str, str, str]], hidden_response: bool = False,
    ) -> List[Result]:
        """
        Send inputs in an interactive fashion; used to handle prompts

        accepts inputs and looks for expected prompt;
        sends the appropriate response, then waits for the "finale"
        returns the results of the interaction

        could be "chained" together to respond to more than a "single" staged prompt

        Args:
            inputs: list or tuple containing strings representing:
                initial input
                expectation (what should ssh2net expect after input)
                response (response to expectation)
                finale (what should ssh2net expect when "done")
            hidden_response: True/False response is hidden (i.e. password input)

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        if not isinstance(inputs, (list, tuple)):
            raise TypeError(f"send_inputs_interact expects a List or Tuple, got {type(inputs)}")
        input_stages = (inputs,)
        results = []
        for channel_input, expectation, response, finale in input_stages:
            result = Result(self.transport.host, channel_input, expectation, response, finale)
            raw_result, processed_result = self._send_input_interact(
                channel_input, expectation, response, finale, hidden_response
            )
            result.raw_result = raw_result.decode()
            result.record_result(processed_result.decode())
            results.append(result)
        return results

    @operation_timeout("timeout_ops")
    def _send_input_interact(
        self,
        channel_input: str,
        expectation: str,
        response: str,
        finale: str,
        hidden_response: bool = False,
    ) -> Tuple[bytes, bytes]:
        """
        Respond to a single "staged" prompt and return results

        Args:
            channel_input: string input to write to channel
            expectation: string of what to expect from channel
            response: string what to respond to the "expectation"
            finale: string of prompt to look for to know when "done"
            hidden_response: True/False response is hidden (i.e. password input)

        Returns:
            output: output read from the channel

        Raises:
            N/A  # noqa

        """
        bytes_channel_input = channel_input.encode()
        self.transport.session_lock.acquire()
        self.transport.flush()
        LOG.debug(
            f"Attempting to send input interact: {channel_input}; "
            f"\texpecting: {expectation};"
            f"\tresponding: {response};"
            f"\twith a finale: {finale};"
            f"\thidden_response: {hidden_response}"
        )
        self.transport.write(channel_input)
        LOG.debug(f"Write: {repr(channel_input)}")
        self._read_until_input(bytes_channel_input)
        self._send_return()
        output = self._read_until_prompt(prompt=expectation)
        # if response is simply a return; add that so it shows in output likewise if response is
        # "hidden" (i.e. password input), add return, otherwise, skip
        if not response:
            output += self.comms_return_char.encode()
        elif hidden_response is True:
            output += self.comms_return_char.encode()
        self.transport.write(response)
        LOG.debug(f"Write: {repr(channel_input)}")
        self._send_return()
        LOG.debug(f"Write (sending return character): {repr(self.comms_return_char)}")
        output += self._read_until_prompt(prompt=finale)
        self.transport.session_lock.release()
        processed_output = self._restructure_output(output, strip_prompt=False)
        return output, processed_output

    def _send_return(self) -> None:
        """
        Send return char to device

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        self.transport.flush()
        self.transport.write(self.comms_return_char)
        LOG.debug(f"Write (sending return character): {repr(self.comms_return_char)}")
