"""Unit tests for fixed-size wire format helpers."""
import pytest

from core import crypto
from core import wire_format

# Import encode/decode functions from event modules
from events.content import message
from events.content import channel
from events.content import channel_update
from events.content import message_update
from events.content import message_deletion
from events.content import message_reaction
from events.content import message_reaction_deletion
from events.content import message_attachment
from events.content import message_rekey
from events.content import file_slice
from events.group import group
from events.group import group_member
from events.group import group_key
from events.group import group_key_shared
from events.group import group_prekey
from events.group import group_prekey_shared
from events.identity import user
from events.identity import username_update
from events.identity import peer
from events.identity import peer_shared
from events.identity import network
from events.identity import admin
from events.identity import invite
from events.identity import invite_accepted
from events.network import connection_prekey
from events.network import connection_prekey_shared
from events.network import connection_request
from events.network import connection_ack
from events.network import negentropy


def test_header_roundtrip():
    signer_id = b"\x11" * wire_format.SIGNER_ID_SIZE
    header = wire_format.WireHeader(
        version=1,
        event_type=wire_format.TYPE_MESSAGE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.SIGNER_PEER_SHARED,
        count=0,
        created_at_ms=123,
        ttl_ms=0,
        signer_id=signer_id,
    )
    packed = header.pack()
    assert len(packed) == wire_format.HEADER_SIZE
    unpacked = wire_format.WireHeader.unpack(packed)
    assert unpacked == header


def test_envelope_roundtrip():
    signer_id = b"\x22" * wire_format.SIGNER_ID_SIZE
    header = wire_format.WireHeader(
        version=1,
        event_type=wire_format.TYPE_MESSAGE,
        flags=0,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=0,
        ttl_ms=0,
        signer_id=signer_id,
    )
    payload = b"\x00" * wire_format.PAYLOAD_SIZE
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    envelope = wire_format.build_envelope(header, payload, signature)
    assert len(envelope) == wire_format.WIRE_SIZE
    parsed_header, parsed_payload, parsed_sig = wire_format.parse_envelope(envelope)
    assert parsed_header == header
    assert parsed_payload == payload
    assert parsed_sig == signature


def test_message_payload_roundtrip():
    channel_id = b"\x01" * 16
    author_id = b"\x02" * 16
    content = "hello world"
    disappearing_time_ms = 9000
    encoded = message.encode_plaintext(
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        disappearing_time_ms=disappearing_time_ms,
    )
    decoded = message.decode_plaintext(encoded)
    assert decoded["channel_id"] == channel_id
    assert decoded["author_id"] == author_id
    assert decoded["content"] == content
    assert decoded["disappearing_time_ms"] == disappearing_time_ms


def test_message_payload_rejects_long_content():
    channel_id = b"\x03" * 16
    author_id = b"\x04" * 16
    content = "x" * (message.CONTENT_MAX + 1)
    with pytest.raises(ValueError):
        message.encode_plaintext(
            channel_id=channel_id,
            author_id=author_id,
            content=content,
            disappearing_time_ms=0,
        )


def test_message_payload_encrypt_roundtrip():
    channel_id = b"\x05" * 16
    author_id = b"\x06" * 16
    content = "wire encryption"
    plaintext = message.encode_plaintext(
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        disappearing_time_ms=0,
    )
    key_data = {
        "id": crypto.hash(b"wire-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = message._encrypt_payload(plaintext, key_data)
    recovered = message._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_channel_payload_roundtrip():
    group_id = b"\x07" * 16
    admin_grant_id = b"\x08" * 16
    name = "general"
    disappearing_time_ms = 1234
    encoded = channel.encode_plaintext(
        group_id=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=1,
        admin_grant_id=admin_grant_id,
    )
    decoded = channel.decode_plaintext(encoded)
    assert decoded["group_id"] == group_id
    assert decoded["name"] == name
    assert decoded["disappearing_time_ms"] == disappearing_time_ms
    assert decoded["is_main"] == 1
    assert decoded["admin_grant_id"] == admin_grant_id


def test_channel_payload_rejects_long_name():
    group_id = b"\x09" * 16
    name = "x" * (channel.NAME_MAX + 1)
    with pytest.raises(ValueError):
        channel.encode_plaintext(
            group_id=group_id,
            name=name,
            disappearing_time_ms=0,
            is_main=0,
            admin_grant_id=None,
        )


def test_channel_payload_encrypt_roundtrip():
    group_id = b"\x0a" * 16
    name = "wire channel"
    plaintext = channel.encode_plaintext(
        group_id=group_id,
        name=name,
        disappearing_time_ms=0,
        is_main=0,
        admin_grant_id=None,
    )
    key_data = {
        "id": crypto.hash(b"wire-channel-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = channel._encrypt_payload(plaintext, key_data)
    recovered = channel._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_message_update_payload_roundtrip():
    message_id = b"\x0b" * 16
    group_id = b"\x0c" * 16
    edited_by = b"\x0d" * 16
    author_id = b"\x0e" * 16
    new_content = "updated"
    encoded = message_update.encode_plaintext(
        message_id=message_id,
        group_id=group_id,
        edited_by=edited_by,
        author_id=author_id,
        new_content=new_content,
    )
    decoded = message_update.decode_plaintext(encoded)
    assert decoded["message_id"] == message_id
    assert decoded["group_id"] == group_id
    assert decoded["edited_by"] == edited_by
    assert decoded["author_id"] == author_id
    assert decoded["new_content"] == new_content


def test_message_update_payload_rejects_long_content():
    message_id = b"\x0f" * 16
    group_id = b"\x10" * 16
    edited_by = b"\x11" * 16
    author_id = b"\x12" * 16
    new_content = "x" * (message_update.UPDATE_MAX + 1)
    with pytest.raises(ValueError):
        message_update.encode_plaintext(
            message_id=message_id,
            group_id=group_id,
            edited_by=edited_by,
            author_id=author_id,
            new_content=new_content,
        )


def test_message_update_payload_encrypt_roundtrip():
    message_id = b"\x13" * 16
    group_id = b"\x14" * 16
    edited_by = b"\x15" * 16
    author_id = b"\x16" * 16
    new_content = "wire update"
    plaintext = message_update.encode_plaintext(
        message_id=message_id,
        group_id=group_id,
        edited_by=edited_by,
        author_id=author_id,
        new_content=new_content,
    )
    key_data = {
        "id": crypto.hash(b"wire-update-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = message_update._encrypt_payload(plaintext, key_data)
    recovered = message_update._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_message_deletion_payload_roundtrip():
    message_id = b"\x17" * 16
    encoded = message_deletion.encode_plaintext(message_id=message_id)
    decoded = message_deletion.decode_plaintext(encoded)
    assert decoded["message_id"] == message_id


def test_message_deletion_payload_encrypt_roundtrip():
    message_id = b"\x18" * 16
    plaintext = message_deletion.encode_plaintext(message_id=message_id)
    key_data = {
        "id": crypto.hash(b"wire-delete-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = message_deletion._encrypt_payload(plaintext, key_data)
    recovered = message_deletion._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_user_payload_roundtrip():
    invite_id = b"\x19" * 16
    user_pubkey = b"\x1a" * wire_format.PUBKEY_SIZE
    network_id = b"\x1b" * 16
    encoded = user.encode_plaintext(
        invite_id=invite_id,
        user_pubkey=user_pubkey,
        network_id=network_id,
    )
    decoded = user.decode_plaintext(encoded)
    assert decoded["invite_id"] == invite_id
    assert decoded["user_pubkey"] == user_pubkey
    assert decoded["network_id"] == network_id


def test_network_payload_roundtrip():
    network_pubkey = b"\x2a" * wire_format.PUBKEY_SIZE
    encoded = network.encode_plaintext(network_pubkey=network_pubkey)
    decoded = network.decode_plaintext(encoded)
    assert decoded["network_pubkey"] == network_pubkey


def test_admin_payload_roundtrip():
    user_id = b"\x2b" * 16
    network_id = b"\x2c" * 16
    admin_grant_id = b"\x2d" * 16
    encoded = admin.encode_plaintext(
        user_id=user_id,
        network_id=network_id,
        admin_grant_id=admin_grant_id,
    )
    decoded = admin.decode_plaintext(encoded)
    assert decoded["user_id"] == user_id
    assert decoded["network_id"] == network_id
    assert decoded["admin_grant_id"] == admin_grant_id


def test_invite_payload_roundtrip():
    invite_pubkey = b"\x2e" * wire_format.PUBKEY_SIZE
    invite_prekey_id = b"\x2f" * 16
    group_id = b"\x30" * 16
    inviter_peer_shared_id = b"\x31" * 16
    inviter_user_id = b"\x32" * 16
    encoded = invite.encode_plaintext(
        mode=invite.INVITE_MODE_USER,
        invite_pubkey=invite_pubkey,
        invite_prekey_id=invite_prekey_id,
        group_id=group_id,
        channel_id=None,
        key_id=None,
        network_id=None,
        inviter_peer_shared_id=inviter_peer_shared_id,
        inviter_user_id=inviter_user_id,
        target_user_id=None,
        admin_grant_id=None,
        inviter_ip="203.0.113.5",
        inviter_port=6100,
    )
    decoded = invite.decode_plaintext(encoded)
    assert decoded["mode"] == invite.INVITE_MODE_USER
    assert decoded["invite_pubkey"] == invite_pubkey
    assert decoded["invite_prekey_id"] == invite_prekey_id
    assert decoded["group_id"] == group_id
    assert decoded["inviter_peer_shared_id"] == inviter_peer_shared_id
    assert decoded["inviter_user_id"] == inviter_user_id
    assert decoded["inviter_ip"] == "203.0.113.5"
    assert decoded["inviter_port"] == 6100


def test_invite_accepted_payload_roundtrip():
    encoded = invite_accepted.encode_plaintext(
        invite_id=b"\x33" * 16,
        invite_prekey_id=b"\x34" * 16,
        invite_private_key=b"\x35" * wire_format.PRIVKEY_SIZE,
        inviter_peer_shared_id=b"\x36" * 16,
        network_id=b"\x37" * 16,
        channel_id=None,
        key_id=None,
        inviter_connection_prekey_public_key=b"\x38" * wire_format.PUBKEY_SIZE,
        inviter_connection_prekey_shared_id=b"\x39" * 16,
        inviter_connection_prekey_id=b"\x3a" * 16,
        inviter_ip="198.51.100.10",
        inviter_port=9000,
        link_user_id=b"\x3b" * 16,
        inviter_peer_shared_blob_id=b"\x3c" * 16,
    )
    decoded = invite_accepted.decode_plaintext(encoded)
    assert decoded["invite_id"] == b"\x33" * 16
    assert decoded["invite_prekey_id"] == b"\x34" * 16
    assert decoded["invite_private_key"] == b"\x35" * wire_format.PRIVKEY_SIZE
    assert decoded["inviter_peer_shared_id"] == b"\x36" * 16
    assert decoded["network_id"] == b"\x37" * 16
    assert decoded["inviter_connection_prekey_shared_id"] == b"\x39" * 16
    assert decoded["inviter_connection_prekey_id"] == b"\x3a" * 16
    assert decoded["inviter_ip"] == "198.51.100.10"
    assert decoded["inviter_port"] == 9000
    assert decoded["link_user_id"] == b"\x3b" * 16
    assert decoded["inviter_peer_shared_blob_id"] == b"\x3c" * 16


def test_negentropy_payload_roundtrip():
    plaintext = negentropy.encode_plaintext(
        connection_id=b"\x3d" * 16,
        reply_connection_id=b"\x3e" * 16,
        msg_type=negentropy.MSG_RANGE_EVENTS,
        range_id=b"\x3f" * negentropy.RANGE_ID_SIZE,
        level=negentropy.LEVEL_PREFIX_4,
        prefix_bytes=b"\x01\x02",
        hash_bytes=b"\x04" * 16,
        root_hash=b"\x05" * 16,
        total_events=123,
        parent_range_id=b"\x06" * negentropy.RANGE_ID_SIZE,
        event_ids=[b"\x07" * 16, b"\x08" * 16],
    )
    decoded = negentropy.decode_plaintext(plaintext)
    assert decoded["connection_id"] == b"\x3d" * 16
    assert decoded["reply_connection_id"] == b"\x3e" * 16
    assert decoded["msg_type"] == negentropy.MSG_RANGE_EVENTS
    assert decoded["range_id"] == b"\x3f" * negentropy.RANGE_ID_SIZE
    assert decoded["level"] == negentropy.LEVEL_PREFIX_4
    assert decoded["prefix_bytes"] == b"\x01\x02"
    assert decoded["total_events"] == 123
    assert decoded["event_ids"] == [b"\x07" * 16, b"\x08" * 16]


def test_username_update_payload_roundtrip():
    user_id = b"\x1c" * 16
    name = "alice"
    encoded = username_update.encode_plaintext(user_id=user_id, name=name)
    decoded = username_update.decode_plaintext(encoded)
    assert decoded["user_id"] == user_id
    assert decoded["name"] == name


def test_username_update_payload_encrypt_roundtrip():
    user_id = b"\x1d" * 16
    name = "bob"
    plaintext = username_update.encode_plaintext(user_id=user_id, name=name)
    key_data = {
        "id": crypto.hash(b"wire-username-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = username_update._encrypt_payload(plaintext, key_data)
    recovered = username_update._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_peer_payload_roundtrip():
    public_key = b"\x1e" * wire_format.PUBKEY_SIZE
    private_key = b"\x1f" * wire_format.PRIVKEY_SIZE
    encoded = peer.encode_plaintext(public_key=public_key, private_key=private_key)
    decoded = peer.decode_plaintext(encoded)
    assert decoded["public_key"] == public_key
    assert decoded["private_key"] == private_key


def test_peer_shared_payload_roundtrip():
    public_key = b"\x20" * wire_format.PUBKEY_SIZE
    peer_id = b"\x21" * 16
    invite_id = b"\x22" * 16
    encoded = peer_shared.encode_plaintext(
        public_key=public_key,
        peer_id=peer_id,
        invite_id=invite_id,
    )
    decoded = peer_shared.decode_plaintext(encoded)
    assert decoded["public_key"] == public_key
    assert decoded["peer_id"] == peer_id
    assert decoded["invite_id"] == invite_id


def test_message_reaction_payload_roundtrip():
    message_id = b"\x40" * 16
    reactor_id = b"\x41" * 16
    emoji = chr(0x1F600)
    encoded = message_reaction.encode_plaintext(
        message_id=message_id,
        reactor_id=reactor_id,
        emoji=emoji,
    )
    decoded = message_reaction.decode_plaintext(encoded)
    assert decoded["message_id"] == message_id
    assert decoded["reactor_id"] == reactor_id
    assert decoded["emoji"] == emoji


def test_message_reaction_payload_encrypt_roundtrip():
    message_id = b"\x42" * 16
    reactor_id = b"\x43" * 16
    plaintext = message_reaction.encode_plaintext(
        message_id=message_id,
        reactor_id=reactor_id,
        emoji=chr(0x1F60E),
    )
    key_data = {
        "id": crypto.hash(b"wire-reaction-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = message_reaction._encrypt_payload(plaintext, key_data)
    recovered = message_reaction._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_message_reaction_deletion_payload_roundtrip():
    reaction_id = b"\x44" * 16
    encoded = message_reaction_deletion.encode_plaintext(reaction_id=reaction_id)
    decoded = message_reaction_deletion.decode_plaintext(encoded)
    assert decoded["reaction_id"] == reaction_id


def test_message_attachment_payload_roundtrip():
    message_id = b"\x45" * 16
    file_id = b"\x46" * 16
    nonce_prefix = b"\x01" * wire_format.NONCE_PREFIX_SIZE
    enc_key = b"\x02" * wire_format.SECRET_SIZE
    root_hash = b"\x03" * 32
    encoded = message_attachment.encode_plaintext(
        message_id=message_id,
        file_id=file_id,
        blob_bytes=1234,
        total_slices=5,
        nonce_prefix=nonce_prefix,
        enc_key=enc_key,
        root_hash=root_hash,
        filename="report.pdf",
        mime_type="application/pdf",
    )
    decoded = message_attachment.decode_plaintext(encoded)
    assert decoded["message_id"] == message_id
    assert decoded["file_id"] == file_id
    assert decoded["blob_bytes"] == 1234
    assert decoded["total_slices"] == 5
    assert decoded["nonce_prefix"] == nonce_prefix
    assert decoded["enc_key"] == enc_key
    assert decoded["root_hash"] == root_hash
    assert decoded["filename"] == "report.pdf"
    assert decoded["mime_type"] == "application/pdf"


def test_message_attachment_payload_encrypt_roundtrip():
    plaintext = message_attachment.encode_plaintext(
        message_id=b"\x47" * 16,
        file_id=b"\x48" * 16,
        blob_bytes=10,
        total_slices=1,
        nonce_prefix=b"\x04" * wire_format.NONCE_PREFIX_SIZE,
        enc_key=b"\x05" * wire_format.SECRET_SIZE,
        root_hash=b"\x06" * 32,
        filename="a.txt",
        mime_type="text/plain",
    )
    key_data = {
        "id": crypto.hash(b"wire-attach-key"),
        "key": crypto.generate_secret(),
        "type": "symmetric",
    }
    payload = message_attachment._encrypt_payload(plaintext, key_data)
    recovered = message_attachment._decrypt_payload(payload, key_data)
    assert recovered == plaintext


def test_message_rekey_payload_roundtrip():
    original_message_id = b"\x49" * 16
    new_key_id = b"\x4a" * 16
    ciphertext = b"\x05" * 32
    encoded = message_rekey.encode_plaintext(
        original_message_id=original_message_id,
        new_key_id=new_key_id,
        new_ciphertext=ciphertext,
    )
    decoded = message_rekey.decode_plaintext(encoded)
    assert decoded["original_message_id"] == original_message_id
    assert decoded["new_key_id"] == new_key_id
    assert decoded["new_ciphertext"] == ciphertext


def test_channel_update_payload_roundtrip():
    channel_id = b"\x4b" * 16
    group_id = b"\x4c" * 16
    updated_by = b"\x4d" * 16
    encoded = channel_update.encode_plaintext(
        channel_id=channel_id,
        group_id=group_id,
        updated_by=updated_by,
        new_channel_name="news",
        new_disappearing_time_ms=None,
    )
    decoded = channel_update.decode_plaintext(encoded)
    assert decoded["channel_id"] == channel_id
    assert decoded["group_id"] == group_id
    assert decoded["updated_by"] == updated_by
    assert decoded["new_channel_name"] == "news"
    assert decoded["new_disappearing_time_ms"] is None


def test_group_payload_roundtrip():
    key_id = b"\x4e" * 16
    network_id = b"\x4f" * 16
    encoded = group.encode_plaintext(
        name="team",
        key_id=key_id,
        is_main=1,
        network_id=network_id,
    )
    decoded = group.decode_plaintext(encoded)
    assert decoded["name"] == "team"
    assert decoded["key_id"] == key_id
    assert decoded["is_main"] == 1
    assert decoded["network_id"] == network_id


def test_group_member_payload_roundtrip():
    group_id = b"\x50" * 16
    user_id = b"\x51" * 16
    added_by = b"\x52" * 16
    admin_grant_id = b"\x53" * 16
    encoded = group_member.encode_plaintext(
        group_id=group_id,
        user_id=user_id,
        added_by=added_by,
        admin_grant_id=admin_grant_id,
    )
    decoded = group_member.decode_plaintext(encoded)
    assert decoded["group_id"] == group_id
    assert decoded["user_id"] == user_id
    assert decoded["added_by"] == added_by
    assert decoded["admin_grant_id"] == admin_grant_id


def test_group_key_payload_roundtrip():
    key = b"\x54" * wire_format.SECRET_SIZE
    encoded = group_key.encode_plaintext(key=key)
    decoded = group_key.decode_plaintext(encoded)
    assert decoded["key"] == key


def test_group_key_shared_payload_roundtrip():
    key_id = b"\x55" * 16
    symmetric_key = b"\x56" * wire_format.SECRET_SIZE
    recipient_prekey_id = b"\x57" * 16
    encoded = group_key_shared.encode_plaintext(
        key_id=key_id,
        symmetric_key=symmetric_key,
        recipient_prekey_id=recipient_prekey_id,
    )
    decoded = group_key_shared.decode_plaintext(encoded)
    assert decoded["key_id"] == key_id
    assert decoded["symmetric_key"] == symmetric_key
    assert decoded["recipient_prekey_id"] == recipient_prekey_id


def test_group_prekey_payload_roundtrip():
    public_key = b"\x58" * wire_format.PUBKEY_SIZE
    private_key = b"\x59" * wire_format.PRIVKEY_SIZE
    encoded = group_prekey.encode_plaintext(public_key=public_key, private_key=private_key)
    decoded = group_prekey.decode_plaintext(encoded)
    assert decoded["public_key"] == public_key
    assert decoded["private_key"] == private_key


def test_group_prekey_shared_payload_roundtrip():
    group_prekey_id = b"\x5a" * 16
    peer_id = b"\x5b" * 16
    public_key = b"\x5c" * wire_format.PUBKEY_SIZE
    encoded = group_prekey_shared.encode_plaintext(
        group_prekey_id=group_prekey_id,
        peer_id=peer_id,
        public_key=public_key,
    )
    decoded = group_prekey_shared.decode_plaintext(encoded)
    assert decoded["group_prekey_id"] == group_prekey_id
    assert decoded["peer_id"] == peer_id
    assert decoded["public_key"] == public_key


def test_connection_prekey_payload_roundtrip():
    public_key = b"\x5d" * wire_format.PUBKEY_SIZE
    private_key = b"\x5e" * wire_format.PRIVKEY_SIZE
    encoded = connection_prekey.encode_plaintext(public_key=public_key, private_key=private_key)
    decoded = connection_prekey.decode_plaintext(encoded)
    assert decoded["public_key"] == public_key
    assert decoded["private_key"] == private_key


def test_connection_prekey_shared_payload_roundtrip():
    connection_prekey_id = b"\x5f" * 16
    peer_id = b"\x60" * 16
    public_key = b"\x61" * wire_format.PUBKEY_SIZE
    encoded = connection_prekey_shared.encode_plaintext(
        connection_prekey_id=connection_prekey_id,
        peer_id=peer_id,
        public_key=public_key,
    )
    decoded = connection_prekey_shared.decode_plaintext(encoded)
    assert decoded["connection_prekey_id"] == connection_prekey_id
    assert decoded["peer_id"] == peer_id
    assert decoded["public_key"] == public_key


def test_connection_request_payload_roundtrip():
    key = b"\x62" * wire_format.SECRET_SIZE
    to_peer_shared_id = b"\x63" * 16
    invite_id = b"\x64" * 16
    encoded = connection_request.encode_plaintext(
        key=key,
        to_peer_shared_id=to_peer_shared_id,
        invite_id=invite_id,
    )
    decoded = connection_request.decode_plaintext(encoded)
    assert decoded["key"] == key
    assert decoded["to_peer_shared_id"] == to_peer_shared_id
    assert decoded["invite_id"] == invite_id


def test_connection_ack_payload_roundtrip():
    for_request_id = b"\x65" * 16
    key = b"\x66" * wire_format.SECRET_SIZE
    encoded = connection_ack.encode_plaintext(for_request_id=for_request_id, key=key)
    decoded = connection_ack.decode_plaintext(encoded)
    assert decoded["for_request_id"] == for_request_id
    assert decoded["key"] == key


def test_file_slice_roundtrip():
    file_id = b"\x67" * 16
    nonce = b"\x01" * file_slice.FILE_SLICE_NONCE_SIZE
    ciphertext = b"\x02" * 10
    poly_tag = b"\x03" * file_slice.FILE_SLICE_TAG_SIZE
    encoded = file_slice.encode_wire_event(
        file_id=file_id,
        slice_number=7,
        nonce=nonce,
        ciphertext=ciphertext,
        poly_tag=poly_tag,
    )
    decoded = file_slice.decode_wire_event(encoded)
    assert decoded["file_id"] == crypto.b64encode(file_id)
    assert decoded["slice_number"] == 7
    decoded_ciphertext = crypto.b64decode(decoded["ciphertext"])
    assert decoded_ciphertext[:len(ciphertext)] == ciphertext
    assert len(decoded_ciphertext) == file_slice.FILE_SLICE_CIPHERTEXT_SIZE
