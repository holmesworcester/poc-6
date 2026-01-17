---- MODULE EventGraphSchema ----
EXTENDS Naturals

\* Schema-level, bounded model of the full event graph and validation relations.
\* This model abstracts away payloads and crypto, focusing on:
\* - dependency ordering (requires)
\* - signer gating (who must already be valid)
\* - trust-anchor gating for network validity
\*
\* The event nodes include "mode variants" (bootstrap vs ongoing) to make
\* polymorphic signer/dependency rules explicit without introducing extra state.
\*
\* Use ActiveEvents in the TLC config to bound the state space.
\* FullEvents lists all modeled nodes; ActiveEvents chooses a subset.
\* Use Peers in the TLC config to model per-peer perspectives.

CONSTANTS ActiveEvents, Peers

VARIABLES recorded, valid, trustAnchor, trusted

Net == "network"
InviteAccepted == "invite_accepted"

InviteUserBoot == "invite_user_bootstrap"
InviteUserOngoing == "invite_user_ongoing"
InvitePeerFirst == "invite_peer_first"
InvitePeerOngoing == "invite_peer_ongoing"

UserBoot == "user_bootstrap"
UserOngoing == "user_ongoing"

PeerSharedFirst == "peer_shared_first"
PeerSharedOngoing == "peer_shared_ongoing"

AdminBoot == "admin_bootstrap"
AdminOngoing == "admin_ongoing"

Peer == "peer"

GroupKey == "group_key"
GroupAllUsers == "group_all_users"
GroupNormal == "group_normal"
GroupKeyShared == "group_key_shared"
GroupPrekey == "group_prekey"
GroupPrekeyShared == "group_prekey_shared"
GroupMember == "group_member"

Channel == "channel"
ChannelUpdate == "channel_update"

Message == "message"
MessageUpdate == "message_update"
MessageReaction == "message_reaction"
MessageReactionDeletion == "message_reaction_deletion"
MessageDeletion == "message_deletion"
MessageAttachment == "message_attachment"
MessageRekey == "message_rekey"
FileSlice == "file_slice"

UsernameUpdate == "username_update"
PeerNameUpdate == "peer_name_update"
NetworkNameUpdate == "network_name_update"
UserRemoved == "user_removed"
PeerRemoved == "peer_removed"

TransitPrekey == "transit_prekey"
TransitPrekeyShared == "transit_prekey_shared"
ObservedAddress == "observed_address"
SelfAddress == "self_address"
NetworkIntro == "network_intro"
Sync == "sync"
Connection == "connection"

FullEvents == {
    Net, InviteAccepted,
    InviteUserBoot, InviteUserOngoing, InvitePeerFirst, InvitePeerOngoing,
    UserBoot, UserOngoing,
    PeerSharedFirst, PeerSharedOngoing,
    AdminBoot, AdminOngoing,
    Peer,
    GroupKey, GroupAllUsers, GroupNormal, GroupKeyShared, GroupPrekey, GroupPrekeyShared, GroupMember,
    Channel, ChannelUpdate,
    Message, MessageUpdate, MessageReaction, MessageReactionDeletion, MessageDeletion,
    MessageAttachment, MessageRekey, FileSlice,
    UsernameUpdate, PeerNameUpdate, NetworkNameUpdate, UserRemoved, PeerRemoved,
    TransitPrekey, TransitPrekeyShared, ObservedAddress, SelfAddress, NetworkIntro, Sync, Connection
}

LocalRoots == {
    InviteAccepted, Peer, GroupKey, GroupPrekey, TransitPrekey, Connection, FileSlice
}

NetGuardedEvents == (FullEvents \ LocalRoots)

IdentityEvents == {
    Net, InviteAccepted,
    InviteUserBoot, InviteUserOngoing, InvitePeerFirst, InvitePeerOngoing,
    UserBoot, UserOngoing,
    PeerSharedFirst, PeerSharedOngoing,
    AdminBoot, AdminOngoing,
    Peer,
    UserRemoved, PeerRemoved
}

GroupEvents == {
    GroupKey, GroupAllUsers, GroupNormal, GroupKeyShared, GroupPrekey, GroupPrekeyShared, GroupMember
}

ContentEvents == {
    Channel, ChannelUpdate,
    Message, MessageUpdate, MessageReaction, MessageReactionDeletion, MessageDeletion,
    MessageAttachment, MessageRekey, FileSlice,
    UsernameUpdate, PeerNameUpdate, NetworkNameUpdate
}

NetworkEvents == {
    TransitPrekey, TransitPrekeyShared, ObservedAddress, SelfAddress, NetworkIntro, Sync, Connection
}

CoreEvents == {
    Net, InviteAccepted,
    InviteUserBoot, UserBoot,
    InvitePeerFirst, PeerSharedFirst,
    AdminBoot, GroupAllUsers, Channel, Message
}

ASSUME ActiveEvents \subseteq FullEvents
ASSUME Peers /= {}

EVENTS == ActiveEvents

RawDeps(e) ==
    CASE e = Net -> {}
       [] e = InviteAccepted -> {}

       [] e = InviteUserBoot -> {}
       [] e = InviteUserOngoing -> {AdminBoot}
       [] e = InvitePeerFirst -> {}
       [] e = InvitePeerOngoing -> {}

       [] e = UserBoot -> {}
       [] e = UserOngoing -> {}

       [] e = PeerSharedFirst -> {}
       [] e = PeerSharedOngoing -> {}

       [] e = AdminBoot -> {Net}
       [] e = AdminOngoing -> {Net, AdminBoot}

       [] e = Peer -> {}

       [] e = GroupKey -> {}
       [] e = GroupAllUsers -> {GroupKey}
       [] e = GroupNormal -> {GroupKey}
       [] e = GroupKeyShared -> {}
       [] e = GroupPrekey -> {}
       [] e = GroupPrekeyShared -> {}
       [] e = GroupMember -> {GroupAllUsers, UserOngoing, AdminBoot}

       [] e = Channel -> {GroupAllUsers}
       [] e = ChannelUpdate -> {Channel}

       [] e = Message -> {Channel, UserOngoing}
       [] e = MessageUpdate -> {Message}
       [] e = MessageReaction -> {Message}
       [] e = MessageReactionDeletion -> {}
       [] e = MessageDeletion -> {}
       [] e = MessageAttachment -> {Message}
       [] e = MessageRekey -> {Message, GroupKey}
       [] e = FileSlice -> {}

       [] e = UsernameUpdate -> {UserOngoing, GroupKey}
       [] e = PeerNameUpdate -> {PeerSharedOngoing, GroupKey}
       [] e = NetworkNameUpdate -> {Net, GroupKey}
       [] e = UserRemoved -> {UserOngoing}
       [] e = PeerRemoved -> {PeerSharedOngoing}

       [] e = TransitPrekey -> {}
       [] e = TransitPrekeyShared -> {}
       [] e = ObservedAddress -> {}
       [] e = SelfAddress -> {}
       [] e = NetworkIntro -> {}
       [] e = Sync -> {}
       [] e = Connection -> {}
       [] OTHER -> {}

SignerDep(e) ==
    CASE e = InviteUserBoot -> {Net}
       [] e = InviteUserOngoing -> {PeerSharedOngoing}
       [] e = InvitePeerFirst -> {UserBoot}
       [] e = InvitePeerOngoing -> {PeerSharedOngoing}

       [] e = UserBoot -> {InviteUserBoot}
       [] e = UserOngoing -> {InviteUserOngoing}

       [] e = PeerSharedFirst -> {InvitePeerFirst}
       [] e = PeerSharedOngoing -> {InvitePeerOngoing}

       [] e = AdminBoot -> {Net}
       [] e = AdminOngoing -> {PeerSharedOngoing}

       [] e = GroupAllUsers -> {Net}
       [] e = GroupNormal -> {PeerSharedOngoing}
       [] e = GroupKeyShared -> {PeerSharedOngoing}
       [] e = GroupPrekeyShared -> {PeerSharedOngoing}
       [] e = GroupMember -> {PeerSharedOngoing}

       [] e = Channel -> {PeerSharedOngoing}
       [] e = ChannelUpdate -> {PeerSharedOngoing}

       [] e = Message -> {PeerSharedOngoing}
       [] e = MessageUpdate -> {PeerSharedOngoing}
       [] e = MessageReaction -> {PeerSharedOngoing}
       [] e = MessageReactionDeletion -> {PeerSharedOngoing}
       [] e = MessageDeletion -> {PeerSharedOngoing}
       [] e = MessageAttachment -> {PeerSharedOngoing}

       [] e = UsernameUpdate -> {PeerSharedOngoing}
       [] e = PeerNameUpdate -> {PeerSharedOngoing}
       [] e = NetworkNameUpdate -> {PeerSharedOngoing}
       [] e = UserRemoved -> {PeerSharedOngoing}
       [] e = PeerRemoved -> {PeerSharedOngoing}

       [] e = TransitPrekeyShared -> {PeerSharedOngoing}
       [] e = ObservedAddress -> {PeerSharedOngoing}
       [] e = SelfAddress -> {PeerSharedOngoing}
       [] e = NetworkIntro -> {PeerSharedOngoing}
       [] OTHER -> {}

Deps(e) == (RawDeps(e) \cup SignerDep(e)) \cap EVENTS

\* Network validity is gated by an invite_accepted trust anchor (per peer).
Guard(p, e) == IF e = Net THEN trustAnchor[p] ELSE TRUE

Init ==
    /\ recorded = [p \in Peers |-> {}]
    /\ valid = [p \in Peers |-> {}]
    /\ trustAnchor = [p \in Peers |-> FALSE]
    /\ trusted = [p \in Peers |-> {}]

Record(p, e) ==
    /\ p \in Peers
    /\ e \in EVENTS
    /\ e \notin recorded[p]
    /\ recorded' = [recorded EXCEPT ![p] = @ \cup {e}]
    /\ UNCHANGED <<valid, trustAnchor, trusted>>

Project(p, e) ==
    /\ p \in Peers
    /\ e \in recorded[p]
    /\ e \notin valid[p]
    /\ Deps(e) \subseteq valid[p]
    /\ Guard(p, e)
    /\ valid' = [valid EXCEPT ![p] = @ \cup {e}]
    /\ trustAnchor' =
        IF e = InviteAccepted
        THEN [trustAnchor EXCEPT ![p] = TRUE]
        ELSE trustAnchor
    /\ UNCHANGED <<recorded, trusted>>

VerifySlice(p) ==
    /\ p \in Peers
    /\ FileSlice \in valid[p]
    /\ MessageAttachment \in valid[p]
    /\ FileSlice \notin trusted[p]
    /\ trusted' = [trusted EXCEPT ![p] = @ \cup {FileSlice}]
    /\ UNCHANGED <<recorded, valid, trustAnchor>>

Stutter ==
    UNCHANGED <<recorded, valid, trustAnchor, trusted>>

Next ==
    \/ \E p \in Peers, e \in EVENTS: Record(p, e)
    \/ \E p \in Peers, e \in EVENTS: Project(p, e)
    \/ \E p \in Peers: VerifySlice(p)
    \/ Stutter

Spec ==
    Init /\ [][Next]_<<recorded, valid, trustAnchor, trusted>>

TypeOK ==
    /\ recorded \in [Peers -> SUBSET EVENTS]
    /\ valid \in [Peers -> SUBSET EVENTS]
    /\ \A p \in Peers: valid[p] \subseteq recorded[p]
    /\ trustAnchor \in [Peers -> {TRUE, FALSE}]
    /\ trusted \in [Peers -> SUBSET EVENTS]

InvDeps ==
    \A p \in Peers:
        \A e \in valid[p]: Deps(e) \subseteq valid[p]

InvSigner ==
    \A p \in Peers:
        \A e \in valid[p]: (SignerDep(e) \cap EVENTS) \subseteq valid[p]

InvNetAnchor ==
    \A p \in Peers: (Net \in valid[p]) => trustAnchor[p]

InvTrustAnchorSource ==
    IF InviteAccepted \in EVENTS
    THEN \A p \in Peers: trustAnchor[p] => (InviteAccepted \in valid[p])
    ELSE TRUE

InvAllValidRequireNetwork ==
    IF Net \in EVENTS
    THEN \A p \in Peers:
        \A e \in valid[p]:
            (e \notin NetGuardedEvents) \/ (e = Net) \/ (Net \in valid[p])
    ELSE TRUE

InvUserInviteChain ==
    IF InviteUserBoot \in EVENTS \/ InviteUserOngoing \in EVENTS
    THEN \A p \in Peers:
        ((UserBoot \in valid[p]) => (InviteUserBoot \in valid[p]))
        /\ ((UserOngoing \in valid[p]) => (InviteUserOngoing \in valid[p]))
    ELSE TRUE

InvPeerSharedInviteChain ==
    IF InvitePeerFirst \in EVENTS \/ InvitePeerOngoing \in EVENTS
    THEN \A p \in Peers:
        ((PeerSharedFirst \in valid[p]) => (InvitePeerFirst \in valid[p]))
        /\ ((PeerSharedOngoing \in valid[p]) => (InvitePeerOngoing \in valid[p]))
    ELSE TRUE

InvAdminChain ==
    IF AdminOngoing \in EVENTS
    THEN \A p \in Peers: (AdminOngoing \in valid[p]) => (AdminBoot \in valid[p])
    ELSE TRUE

InvGroupAllUsersNetwork ==
    IF GroupAllUsers \in EVENTS
    THEN \A p \in Peers: (GroupAllUsers \in valid[p]) => (Net \in valid[p])
    ELSE TRUE

InvSliceAnchored ==
    IF FileSlice \in EVENTS /\ MessageAttachment \in EVENTS
    THEN \A p \in Peers:
        (FileSlice \in trusted[p]) =>
            (FileSlice \in valid[p] /\ MessageAttachment \in valid[p])
    ELSE TRUE

====
