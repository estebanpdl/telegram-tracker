import msgspec
from abc import ABC, abstractmethod
from typing import Set, Union



class TGMessages(msgspec.Struct):
    channel_name: str | None = None
    channel_id: int | None = None
    message: str | None = None
    cleaned_message: str | None = None
    date: str | None = None
    msg_link: str | None = None
    msg_from_peer: int | None = None # check type 
    msg_from_id: int | None = None # check type
    number_replies: int | None = None
    number_forwards: int | None = None
    is_forward: bool | None = None
    forward_msg_from_peer_type: str | None = None
    forward_msg_from_peer_id: int | None = None
    forward_msg_from_peer_name: str | None = None
    forward_msg_date: str | None = None
    forward_msg_date_string: str | None = None
    forward_msg_link: str | None = None
    is_reply: bool | None = None
    reply_to_msg_id: int | None = None
    reply_msg_link: str | None = None
    contains_media: bool | None = None
    media_type: str | None = None
    has_url: bool | None = None
    url: str | None = None
    domain: str | None = None
    url_title: str | None = None
    url_description: str | None = None
    document_type: str | None = None
    document_id: int | None = None
    document_video_duration: str | None = None
    document_filename: str | None = None
    poll_id: int | None = None
    poll_question: str | None = None
    poll_total_voters: int | None = None
    poll_results: str | None = None
    contact_phone_number: str | None = None
    contact_name: str | None = None
    contact_userid: str | None = None
    geo_type: str | None = None
    lat: int | None = None
    lon: int | None = None
    venue_id: str | None = None
    venue_type: str | None = None
    venue_title: str | None = None
    venue_address: str | None = None
    venue_provider: str | None = None

class TGResponse(msgspec.Struct):
    channel_name: TGMessages



# put lowest level structs up here:
## Peer Id structs
class PeerId(msgspec.Struct):
    _: str

class PeerChannel(PeerId):
    channel_id: int

class PeerUser(PeerId):
    user_id: int

class Fwd_from(msgspec.Struct):
    _: str
    date: str
    imported: bool
    from_id: PeerId
    from_name: PeerId ## may need a set( if null ) operation / check return value of Peer_id
    channel_post: int
    post_author: int
    saved_from_peer: PeerId ## may need a set( if null ) operation
    saved_from_msg_id: int
    saved_from_id: PeerId ## may need a set( if null ) operation
    psa_type: str



# full message struct




