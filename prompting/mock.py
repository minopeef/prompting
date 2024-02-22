import time
import torch
import asyncio
import random
import bittensor as bt
from prompting.protocol import StreamPromptingSynapse, PromptingSynapse

from functools import partial
from typing import Dict, List, Union, AsyncGenerator, Any


class MockTokenizer:
    def __init__(self):
        super().__init__()

        self.role_expr = "<|mock-{role}|>"

    def apply_chat_template(self, messages, **kwargs):
        prompt = ""
        for m in messages:
            role = self.role_expr.format(role=m["role"])
            content = m["content"]
            prompt += f"<|mock-{role}|> {content}\n"

        return "\n".join(prompt)


class MockModel(torch.nn.Module):
    def __init__(self, phrase):
        super().__init__()

        self.tokenizer = MockTokenizer()
        self.phrase = phrase

    def __call__(self, messages):
        return self.forward(messages)

    def forward(self, messages):
        role_tag = self.tokenizer.role_expr.format(role="assistant")
        return f"{role_tag} {self.phrase}"


class MockPipeline:
    @property
    def tokenizer(self):
        return self.model.tokenizer

    def __init__(
        self,
        phrase="Mock llm output",
        model_kwargs=None,
    ):
        super().__init__()

        self.model_kwargs = model_kwargs or {}
        self.model = MockModel(phrase)

    def __repr__(self):
        return f"{self.__class__.__name__}(phrase={self.model.phrase})"

    def __call__(self, messages, **kwargs):
        return self.forward(messages, **kwargs)

    def forward(self, messages, **kwargs):
        output = self.model(messages)
        return self.postprocess(output)

    def postprocess(self, output, **kwargs):
        output = output.split(self.model.tokenizer.role_expr.format(role="assistant"))[
            -1
        ].strip()
        return [{"generated_text": output}]

    def preprocess(self, **kwargs):
        pass


class MockSubtensor(bt.MockSubtensor):
    def __init__(self, netuid, n=16, wallet=None, network="mock"):
        super().__init__(network=network)

        if not self.subnet_exists(netuid):
            self.create_subnet(netuid)

        # Register ourself (the validator) as a neuron at uid=0
        if wallet is not None:
            self.force_register_neuron(
                netuid=netuid,
                hotkey=wallet.hotkey.ss58_address,
                coldkey=wallet.coldkey.ss58_address,
                balance=100000,
                stake=100000,
            )

        # Register n mock neurons who will be miners
        for i in range(1, n + 1):
            self.force_register_neuron(
                netuid=netuid,
                hotkey=f"miner-hotkey-{i}",
                coldkey="mock-coldkey",
                balance=100000,
                stake=100000,
            )


class MockMetagraph(bt.metagraph):
    def __init__(self, netuid, subtensor, network="mock"):
        super().__init__(netuid=netuid, network=network, sync=False)

        self.subtensor = subtensor
        self.sync(subtensor=self.subtensor)

        for axon in self.axons:
            axon.ip = "127.0.0.0"
            axon.port = 8091

        bt.logging.info(f"Metagraph: {self}")
        bt.logging.info(f"Axons: {self.axons}")


class MockStreamMiner:
    """MockStreamMiner is an echo miner"""

    def __init__(self, streaming_batch_size: int, timeout: float):
        self.streaming_batch_size = streaming_batch_size
        self.timeout = timeout

    def forward(
        self, synapse: StreamPromptingSynapse, start_time: float
    ) -> StreamPromptingSynapse:
        def _forward(self, prompt: str, start_time: float, sample: Any):
            buffer = []
            continue_streaming = True

            try:
                for token in prompt.split():  # split on spaces.
                    buffer.append(token)

                    if time.time() - start_time > self.timeout:
                        print(
                            f"⏰ Timeout reached, stopping streaming. {time.time() - self.start_time}"
                        )
                        break

                    if len(buffer) == self.streaming_batch_size:
                        time.sleep(
                            self.timeout * random.uniform(0.2, 0.5)
                        )  # simulate some async processing time
                        yield buffer, continue_streaming
                        buffer = []

                if buffer:
                    continue_streaming = False
                    yield buffer, continue_streaming

            except Exception as e:
                bt.logging.error(f"Error in forward: {e}")

        prompt = synapse.messages[-1]
        token_streamer = partial(_forward, self, prompt, start_time)
        return token_streamer


class MockDendrite(bt.dendrite):
    """
    Replaces a real bittensor network request with a mock request that just returns some static completion for all axons that are passed and adds some random delay.
    """

    def __init__(self, wallet):
        super().__init__(wallet)

    async def call(
        self,
        i: int,
        start_time: float,
        synapse: bt.Synapse = bt.Synapse(),
        timeout: float = 12.0,
        deserialize: bool = True,
    ) -> bt.Synapse:
        """Simulated call method to fill synapses with mock data."""

        # Add some random delay to the response
        process_time_upper_bound = (
            timeout * 2
        )  # meaning roughly 50% of the time we will timeout
        process_time = random.uniform(0, process_time_upper_bound)

        if process_time < timeout:
            synapse.dendrite.process_time = str(time.time() - start_time)
            # Update the status code and status message of the dendrite to match the axon
            synapse.completion = f"Mock miner completion {i}"
            synapse.dendrite.status_code = 200
            synapse.dendrite.status_message = "OK"
            synapse.dendrite.process_time = str(process_time)
        else:
            synapse.completion = ""
            synapse.dendrite.status_code = 408
            synapse.dendrite.status_message = "Timeout"
            synapse.dendrite.process_time = str(timeout)

        # Return the updated synapse object after deserializing if requested
        if deserialize:
            return synapse.deserialize()
        else:
            return synapse

    async def call_stream(
        self,
        synapse: StreamPromptingSynapse,
        timeout: float = 12.0,
        deserialize: bool = True,
    ) -> AsyncGenerator[Any, Any]:
        """
        Yields:
            object: Each yielded object contains a chunk of the arbitrary response data from the Axon.
            bittensor.Synapse: After the AsyncGenerator has been exhausted, yields the final filled Synapse.
        """

        start_time = time.time()
        continue_streaming = True
        response_buffer = []

        miner = MockStreamMiner(streaming_batch_size=12, timeout=timeout)
        token_streamer = miner.forward(synapse, start_time)

        # Simulating the async streaming without using aiohttp post request
        while continue_streaming:
            for buffer, continue_streaming in token_streamer(True):
                response_buffer.extend(buffer)  # buffer is a List[str]

                if not continue_streaming:
                    synapse.completion = " ".join(response_buffer)
                    synapse.dendrite.status_code = 200
                    synapse.dendrite.status_message = "OK"
                    synapse.dendrite.process_time = str(time.time() - start_time)

                    response_buffer = []

                    print("Total time for response:", synapse.dendrite.process_time)
                    break

                elif (time.time() - start_time) > timeout:
                    synapse.completion = " ".join(
                        response_buffer
                    )  # partially completed response buffer
                    synapse.dendrite.status_code = 408
                    synapse.dendrite.status_message = "Timeout"
                    synapse.dendrite.process_time = str(timeout)

                    continue_streaming = False  # to stop the while loop
                    print("Total time for response:", synapse.dendrite.process_time)
                    break

        # Return the updated synapse object after deserializing if requested
        if deserialize:
            yield synapse.deserialize()
        else:
            yield synapse

    async def forward(
        self,
        axons: List[bt.axon],
        synapse: bt.Synapse = bt.Synapse(),
        timeout: float = 12,
        deserialize: bool = True,
        run_async: bool = True,
        streaming: bool = False,
    ):
        if streaming:
            assert isinstance(
                synapse, StreamPromptingSynapse
            ), "Synapse must be a StreamPromptingSynapse object when is_stream is True."
        else:
            assert isinstance(
                synapse, PromptingSynapse
            ), "Synapse must be a PromptingSynapse object when is_stream is False."

        async def query_all_axons(is_stream: bool):
            """Queries all axons for responses."""

            async def single_axon_response(
                i: int, target_axon: Union[bt.AxonInfo, bt.axon]
            ):
                """Queries a single axon for a response."""

                start_time = time.time()
                s = synapse.copy()

                target_axon = (
                    target_axon.info()
                    if isinstance(target_axon, bt.axon)
                    else target_axon
                )

                # Attach some more required data so it looks real
                s = self.preprocess_synapse_for_request(
                    target_axon_info=target_axon, synapse=s, timeout=timeout
                )

                if is_stream:
                    # If in streaming mode, return the async_generator
                    return self.call_stream(
                        synapse=s,  # type: ignore
                        timeout=timeout,
                        deserialize=False,
                    )
                else:
                    return await self.call(
                        i=i,
                        start_time=start_time,
                        synapse=s,  # type: ignore
                        timeout=timeout,
                        deserialize=deserialize,
                    )

            if not run_async:
                return [
                    await single_axon_response(target_axon) for target_axon in axons
                ]  # type: ignore

            # If run_async flag is True, get responses concurrently using asyncio.gather().
            return await asyncio.gather(
                *(
                    single_axon_response(i, target_axon)
                    for i, target_axon in enumerate(axons)
                )
            )

        return await query_all_axons(is_stream=streaming)

    def __str__(self) -> str:
        """
        Returns a string representation of the Dendrite object.

        Returns:
            str: The string representation of the Dendrite object in the format "dendrite(<user_wallet_address>)".
        """
        return "MockDendrite({})".format(self.keypair.ss58_address)
