import curses
import time

from base64 import urlsafe_b64encode
from datetime import datetime
from threading import Lock, Thread

import httpx

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session


from components.contacts import ContactsMenu, ContactsPrompt
from components.logs import Log
from components.messages import MessageEntry, MessageLog
from components.textboxes import Alignment, Textbox
from database.models import Base, Contact, FernetKey, ReceivedExchangeKey
from database.operations import (
    get_contact_keys,
    get_contacts_without_keys,
    get_unmatched_keys,
    store_fetched_data,
    store_posted_exchange_key,
    store_posted_message,
)
from database.schemas.inputs import ContactInputSchema
from database.schemas.outputs import (
    BaseContactOutputSchema,
    ContactOutputSchema,
)
from parser import ClientArgumentParser
from server.operations import fetch_data, post_exchange_key, post_message
from settings import settings
from states import State
from styling import Layout, LayoutMeasure, LayoutUnit, Padding

class App:
    def __init__(
            self,
            engine: Engine,
            signature_key: Ed25519PrivateKey,
            stdscr: curses.window,
            contacts_menu: ContactsMenu,
            message_log: MessageLog,
            message_entry: MessageEntry,
            output_log: Log,
            textboxes: list[Textbox] | None = None,
        ) -> None:
        self.engine = engine
        self.signature_key = signature_key
        self.stdscr = stdscr
        self.contacts_menu = contacts_menu
        self.message_log = message_log
        self.message_entry = message_entry
        self.output_log = output_log
        self.output_log.add_item(
            title='Connecting',
            timestamp=datetime.now(),
            text=(
                f'Attempting to connect to the server at '
                f'{settings.server.url.base_url}...'
            ),
        )
        self.windows = [contacts_menu, message_log, message_entry, output_log]
        if textboxes:
            self.windows += textboxes
        self.focus_index = 0
        if self.contacts_menu.contacts:
            self.selected_contact = self.contacts_menu.contacts[0]
            self.message_log.set_contact(self.selected_contact)
            self.message_entry.set_contact(self.selected_contact)
        else:
            self.selected_contact = None
        self.connected = False
        self.database_write_lock = Lock()
        self.message_log_write_lock = Lock()
        self.output_log_write_lock = Lock()

    def _ping_server(self, client: httpx.Client) -> bool:
        try:
            client.get(
                url=settings.server.url.ping_url,
                timeout=settings.server.ping_timeout,
            )
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Connection Established',
                    timestamp=datetime.now(),
                    text='Successfully connected to the server.',
                )
            return True
        except Exception:
            return False

    def _fetch_handler(self, client: httpx.Client) -> None:
        contact_keys = get_contact_keys(self.engine)
        if not contact_keys:
            return
        try:
            response = fetch_data(client, self.signature_key, contact_keys)
            with self.database_write_lock:
                store_fetched_data(self.engine, response)
            with self.message_log_write_lock:
                self.message_log.update()
        except httpx.HTTPStatusError as e:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Bad Response',
                    timestamp=datetime.now(),
                    text=str(e),
                )

    def _key_response_handler(self, client: httpx.Client) -> None:
        unmatched_keys = get_unmatched_keys(self.engine)
        for key in unmatched_keys:
            private_key = X25519PrivateKey.generate()
            shared_secret = private_key.exchange(key.public_key)
            encoded_shared_secret = urlsafe_b64encode(shared_secret)
            try:
                response = post_exchange_key(
                    client=client,
                    signature_key=self.signature_key,
                    recipient_public_key=key.contact.verification_key,
                    exchange_key=private_key.public_key(),
                    initial_exchange_key=key.public_key,
                )
            except httpx.HTTPStatusError as e:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Bad Response',
                        timestamp=datetime.now(),
                        text=str(e),
                    )
                continue
            with self.database_write_lock:
                with Session(self.engine) as session:
                    matched_key = session.get_one(ReceivedExchangeKey, key.id)
                    matched_key.matched = True
                    session.add(
                        FernetKey(
                            encoded_bytes=encoded_shared_secret.decode(),
                            contact_id=key.contact.id,
                            timestamp=response.data.timestamp,
                        )
                    )
                    session.commit()

    def _new_contact_key_handler(self, client: httpx.Client):
        new_contacts = get_contacts_without_keys(self.engine)
        for contact in new_contacts:
            self._post_exchange_key(client, contact)

    def _run_server_operations(self):
        client = httpx.Client()
        while True:
            if not self.connected:
                self.connected = self._ping_server(client)
                continue
            try:
                self._fetch_handler(client)
                self._key_response_handler(client)
                self._new_contact_key_handler(client)
            except httpx.TimeoutException:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Server Connection Error',
                        timestamp=datetime.now(),
                        text='Request timed out. Attempting to reconnect...',
                    )
                self.connected = False
            except Exception as e:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Unhandled Server Error',
                        timestamp=datetime.now(),
                        text=str(e),
                    )
            time.sleep(settings.server.fetch_interval)

    def _standard_state_handler(self, key: int) -> State:
        match key:
            case 1:   # Ctrl-A
                return State.ADD_CONTACT
            case 9:   # Tab
                return State.NEXT_WINDOW
            case curses.KEY_BTAB:
                return State.PREV_WINDOW
            case curses.KEY_RESIZE:
                return State.RESIZE
            case 27:  # Esc
                return State.TERMINATE
            case _:
                return self.windows[self.focus_index].handle_key(key)

    def _add_contact(self, client: httpx.Client) -> None:
        self.stdscr.clear()
        self.stdscr.refresh()
        prompt = ContactsPrompt()
        prompt.place(self.stdscr)
        state = State.PROMPT_ACTIVE
        while state == State.PROMPT_ACTIVE:
            if prompt.draw_required:
                prompt.draw()
                prompt.draw_required = False
            key = self.stdscr.getch()
            if key == curses.KEY_RESIZE:
                prompt.place(self.stdscr)
            else:
                state = prompt.handle_key(key)
        if state == State.PROMPT_SUBMITTED:
            name, public_key = prompt.retrieve_contact()
            contact = ContactInputSchema.model_validate({
                'name': name,
                'verification_key': public_key,
            })
            try:
                with self.database_write_lock:
                    with Session(self.engine) as session:
                        obj = Contact(**contact.model_dump())
                        session.add(obj)
                        session.flush()
                        contact = BaseContactOutputSchema.model_validate(obj)
                        session.commit()
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Add Contact Success',
                        timestamp=datetime.now(),
                        text=f"Added new contact '{name}'.",
                    )
                if self.selected_contact is None:
                    self.selected_contact = contact
                    self.message_log.set_contact(contact)
                    self.message_entry.set_contact(contact)
            except Exception as e:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Add Contact Error',
                        timestamp=datetime.now(),
                        text=str(e),
                    )
        self.stdscr.erase()
        self.stdscr.refresh()
        for window in self.windows:
            window.draw_required = True
        self.contacts_menu.refresh()

    def _post_exchange_key(
            self,
            client: httpx.Client,
            contact: BaseContactOutputSchema,
        ) -> None:
        if not self.connected:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Exchange Key Post Error - No Connection',
                    timestamp=datetime.now(),
                    text=(
                        'No exchange keys can be sent until a connection to '
                        'the server is established.'
                    ),
                )
            return
        private_exchange_key = X25519PrivateKey.generate()
        try:
            post_exchange_key(
                client=client,
                signature_key=self.signature_key,
                recipient_public_key=contact.verification_key,
                exchange_key=private_exchange_key.public_key(),
            )
            with self.database_write_lock:
                store_posted_exchange_key(
                    engine=self.engine,
                    contact_id=contact.id,
                    private_exchange_key=private_exchange_key,
                )
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Exchange Key Post Success',
                    timestamp=datetime.now(),
                    text=f"Posted exchange key to {contact.name}.",
                )
        except httpx.TimeoutException:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Exchange Key Post Error',
                    timestamp=datetime.now(),
                    text='Exchange key post request timed out.',
                )
        except httpx.HTTPStatusError as e:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Exchange Key Post Error - Bad Response',
                    timestamp=datetime.now(),
                    text=str(e),
                )
        except Exception as e:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Exchange Key Post Error - Unhandled Exception',
                    timestamp=datetime.now(),
                    text=str(e),
                )

    def _post_message(self, client: httpx.Client) -> None:
        if self.selected_contact is None or not self.message_entry.input:
            return
        elif not self.connected:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Message Post Error - No Connection',
                    timestamp=datetime.now(),
                    text=(
                        'No messages can be sent until a connection to the '
                        'server is established.'
                    ),
                )
            return
        with Session(self.engine) as session:
            obj = session.get(Contact, self.selected_contact.id)
        if obj is None:
            return
        selected_contact = ContactOutputSchema.model_validate(obj)
        if selected_contact.fernet_keys:
            fernet_key = selected_contact.fernet_keys[0].key
            plaintext = self.message_entry.input
            try:
                response = post_message(
                    client=client,
                    signature_key=self.signature_key,
                    recipient_public_key=selected_contact.verification_key,
                    encrypted_text=fernet_key.encrypt(plaintext.encode()),
                )
                with self.database_write_lock:
                    store_posted_message(
                        engine=self.engine,
                        plaintext=plaintext,
                        contact_id=selected_contact.id,
                        response=response,
                    )
                self.message_entry.input = ''
                self.message_entry.cursor_index = 0
                self.message_entry.draw_required = True
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Message Post Success',
                        timestamp=datetime.now(),
                        text=f'Message sent to {self.selected_contact.name}.',
                    )
                with self.message_log_write_lock:
                    self.message_log.update()
            except httpx.TimeoutException:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Message Post Error - Request Timed Out',
                        timestamp=datetime.now(),
                        text='Message post request timed out.',
                    )
            except httpx.HTTPStatusError as e:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Message Post Error - Bad Response',
                        timestamp=datetime.now(),
                        text=str(e),
                    )
            except Exception as e:
                with self.output_log_write_lock:
                    self.output_log.add_item(
                        title='Message Post Error - Unhandled Exception',
                        timestamp=datetime.now(),
                        text=str(e),
                    )
        else:
            with self.output_log_write_lock:
                self.output_log.add_item(
                    title='Message Post Error - No Encryption Key',
                    timestamp=datetime.now(),
                    text=(
                        f'No messages can be posted before a successful key '
                        f'exchange with {selected_contact.name}.'
                    ),
                )

    def _loop_iteration(self, state: State, client: httpx.Client) -> State:
        for index, window in enumerate(self.windows):
            if window.draw_required:
                window.draw(index == self.focus_index)
                window.draw_required = False
        match state:
            case State.STANDARD:
                return self._standard_state_handler(self.stdscr.getch())
            case State.NEXT_WINDOW:
                self.windows[self.focus_index].draw_required = True
                for _ in range(len(self.windows)):
                    self.focus_index += 1
                    if self.focus_index >= len(self.windows):
                        self.focus_index = 0
                    if self.windows[self.focus_index].focusable:
                        self.windows[self.focus_index].draw_required = True
                        break
            case State.PREV_WINDOW:
                self.windows[self.focus_index].draw_required = True
                for _ in range(len(self.windows)):
                    self.focus_index -= 1
                    if self.focus_index < 0:
                        self.focus_index = len(self.windows) - 1
                    if self.windows[self.focus_index].focusable:
                        self.windows[self.focus_index].draw_required = True
                        break
            case State.RESIZE:
                self.stdscr.clear()
                self.stdscr.refresh()
                for window in self.windows:
                    window.place(self.stdscr)
            case State.ADD_CONTACT:
                self._add_contact(client)
            case State.SELECT_CONTACT:
                if self.contacts_menu.contacts:
                    self.selected_contact = self.contacts_menu.current_contact
                    self.message_log.set_contact(self.selected_contact)
                    self.message_entry.set_contact(self.selected_contact)
            case State.SEND_EXCHANGE_KEY:
                if self.contacts_menu.contacts:
                    self._post_exchange_key(
                        client=client,
                        contact=self.contacts_menu.current_contact,
                    )
            case State.SEND_MESSAGE:
                self._post_message(client)
            case _:
                pass

        return State.STANDARD

    def run(self):
        # Initiate all secondary threads.
        Thread(target=self._run_server_operations, daemon=True).start()
        # Set up the initial state and begin the main loop.
        state = State.STANDARD
        client = httpx.Client(timeout=settings.server.request_timeout)
        self.stdscr.keypad(True)
        self.stdscr.nodelay(True)
        try:
            self.stdscr.clear()
            self.stdscr.refresh()
            for window in self.windows:
                window.place(self.stdscr)
            while state != State.TERMINATE:
                state = self._loop_iteration(state, client)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    parser = ClientArgumentParser()
    signature_key = parser.signature_key
    public_key = signature_key.public_key()
    public_key_b64 = urlsafe_b64encode(public_key.public_bytes_raw()).decode()
    engine = create_engine(settings.local_database.url)
    Base.metadata.create_all(engine)
    def main(stdscr: curses.window):
        app = App(
            engine,
            signature_key,
            stdscr,
            ContactsMenu(
                engine=engine,
                layout=Layout(
                    height=LayoutMeasure(
                        (75, LayoutUnit.PERCENTAGE),
                    ),
                    width=LayoutMeasure(
                        (20, LayoutUnit.PERCENTAGE),
                    ),
                    top=LayoutMeasure(),
                    left=LayoutMeasure(),
                ),
                padding=Padding(1),
            ),
            MessageLog(
                engine=engine,
                contact=None,
                layout=Layout(
                    height=LayoutMeasure(
                        (75, LayoutUnit.PERCENTAGE),
                        (-4, LayoutUnit.CHARS),
                    ),
                    width=LayoutMeasure(
                        (80, LayoutUnit.PERCENTAGE),
                    ),
                    top=LayoutMeasure(),
                    left=LayoutMeasure(
                        (20, LayoutUnit.PERCENTAGE),
                    ),
                ),
                padding=Padding(1),
            ),
            MessageEntry(
                engine=engine,
                contact=None,
                layout=Layout(
                    height=LayoutMeasure(
                        (4, LayoutUnit.CHARS),
                    ),
                    width=LayoutMeasure(
                        (80, LayoutUnit.PERCENTAGE),
                    ),
                    top=LayoutMeasure(
                        (75, LayoutUnit.PERCENTAGE),
                        (-4, LayoutUnit.CHARS),
                    ),
                    left=LayoutMeasure(
                        (20, LayoutUnit.PERCENTAGE),
                    ),
                ),
                padding=Padding(0, 1),
            ),
            Log(
                layout=Layout(
                    height=LayoutMeasure(
                        (25, LayoutUnit.PERCENTAGE),
                        (-4, LayoutUnit.CHARS),
                    ),
                    width=LayoutMeasure(
                        (100, LayoutUnit.PERCENTAGE),
                    ),
                    top=LayoutMeasure(
                        (75, LayoutUnit.PERCENTAGE),
                    ),
                    left=LayoutMeasure(),
                ),
                padding=Padding(0, 1),
            ),
            [
                Textbox(
                    text_lines=[
                        'Controls:',
                        ' | '.join([
                            'Ctrl-A: Add Contact',
                            'Esc: Close',
                        ]),
                    ],
                    alignment=Alignment.LEFT,
                    layout=Layout(
                        height=LayoutMeasure(
                            (4, LayoutUnit.CHARS),
                        ),
                        width=LayoutMeasure(
                            (100, LayoutUnit.PERCENTAGE),
                            (-48, LayoutUnit.CHARS),
                        ),
                        top=LayoutMeasure(
                            (100, LayoutUnit.PERCENTAGE),
                            (-4, LayoutUnit.CHARS),
                        ),
                        left=LayoutMeasure(),
                    ),
                    padding=Padding(1, 2),
                    attributes=[curses.A_BOLD],
                ),
                Textbox(
                    text_lines=[
                        'Your public key:',
                        public_key_b64,
                    ],
                    alignment=Alignment.LEFT,
                    layout=Layout(
                        height=LayoutMeasure(
                            (4, LayoutUnit.CHARS),
                        ),
                        width=LayoutMeasure(
                            (48, LayoutUnit.CHARS),
                        ),
                        top=LayoutMeasure(
                            (100, LayoutUnit.PERCENTAGE),
                            (-4, LayoutUnit.CHARS),
                        ),
                        left=LayoutMeasure(
                            (100, LayoutUnit.PERCENTAGE),
                            (-48, LayoutUnit.CHARS),
                        ),
                    ),
                    padding=Padding(1, 2),
                    attributes=[curses.A_BOLD],
                ),
            ],
        )
        app.run()
    curses.wrapper(main)
