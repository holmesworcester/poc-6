---- MODULE BootstrapGraph ----
EXTENDS Naturals

\* Model of the user-auth invite chain and bootstrap connection upgrade.
\* Focuses on v2 projection semantics (EVENT_SPEC deps + signer gating),
\* plus the bootstrap connection path that uses invite_accepteds.
\*
\* Sources:
\* - docs/planning/network-root-linking-design.md
\* - tests/v2_projectors/test_identity_chain.py

VARIABLES recorded, valid, trustAnchor, inviteAcceptedMode, connInvite, connPeer

Net == "network"
InviteUserBoot == "invite_user_bootstrap"
UserAlice == "user_alice"
AdminGrantAlice == "admin_grant_alice"
InvitePeerAlice == "invite_peer_alice"
PeerSharedAlice == "peer_shared_alice"
InviteUserOngoing == "invite_user_ongoing"
InviteAcceptedBob == "invite_accepted_bob"
UserBob == "user_bob"
InvitePeerBob == "invite_peer_bob"
PeerSharedBob == "peer_shared_bob"

EVENTS == {
    Net, InviteUserBoot, UserAlice, AdminGrantAlice,
    InvitePeerAlice, PeerSharedAlice, InviteUserOngoing,
    InviteAcceptedBob, UserBob, InvitePeerBob, PeerSharedBob
}

Deps(e) ==
    CASE e = Net -> {}
       [] e = InviteUserBoot -> {Net}
       [] e = UserAlice -> {InviteUserBoot}
       [] e = AdminGrantAlice -> {Net}
       [] e = InvitePeerAlice -> {UserAlice}
       [] e = PeerSharedAlice -> {InvitePeerAlice}
       [] e = InviteUserOngoing -> {PeerSharedAlice, AdminGrantAlice}
       [] e = InviteAcceptedBob -> {}
       [] e = UserBob -> {InviteUserOngoing}
       [] e = InvitePeerBob -> {UserBob}
       [] e = PeerSharedBob -> {InvitePeerBob}
       [] OTHER -> {}

Guard(e) == TRUE

Init ==
    /\ recorded = {}
    /\ valid = {}
    /\ trustAnchor = FALSE
    /\ inviteAcceptedMode = "none"
    /\ connInvite = FALSE
    /\ connPeer = FALSE

Record(e) ==
    /\ e \in EVENTS
    /\ e \notin recorded
    /\ recorded' = recorded \cup {e}
    /\ UNCHANGED <<valid, trustAnchor, inviteAcceptedMode, connInvite, connPeer>>

Project(e) ==
    /\ e \in recorded
    /\ e \notin valid
    /\ Deps(e) \subseteq valid
    /\ Guard(e)
    /\ valid' = valid \cup {e}
    /\ trustAnchor' = IF e = InviteAcceptedBob THEN TRUE ELSE trustAnchor
    /\ (e = InviteAcceptedBob => inviteAcceptedMode = "none" /\ inviteAcceptedMode' \in {"user", "peer"})
    /\ (e /= InviteAcceptedBob => inviteAcceptedMode' = inviteAcceptedMode)
    /\ UNCHANGED <<recorded, connInvite, connPeer>>

\* Device linking: invite_accepted without network_id forces peer invite validity.
TrustPeerInvite ==
    /\ InviteAcceptedBob \in valid
    /\ inviteAcceptedMode = "peer"
    /\ InvitePeerBob \notin valid
    /\ valid' = valid \cup {InvitePeerBob}
    /\ UNCHANGED <<recorded, trustAnchor, inviteAcceptedMode, connInvite, connPeer>>

\* Bootstrap connection: invite-labeled connection before peer_shared is known.
ConnectByInvite ==
    /\ ~connInvite
    /\ InviteAcceptedBob \in valid
    /\ InviteUserOngoing \in recorded
    /\ connInvite' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, inviteAcceptedMode, connPeer>>

\* Upgrade to peer_shared-labeled connection once both peers are known.
UpgradeToPeer ==
    /\ connInvite
    /\ ~connPeer
    /\ PeerSharedAlice \in valid
    /\ PeerSharedBob \in valid
    /\ connPeer' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, inviteAcceptedMode, connInvite>>

Stutter ==
    UNCHANGED <<recorded, valid, trustAnchor, inviteAcceptedMode, connInvite, connPeer>>

Next ==
    \/ \E e \in EVENTS: Record(e)
    \/ \E e \in EVENTS: Project(e)
    \/ TrustPeerInvite
    \/ ConnectByInvite
    \/ UpgradeToPeer
    \/ Stutter

Spec ==
    Init /\ [][Next]_<<recorded, valid, trustAnchor, inviteAcceptedMode, connInvite, connPeer>>

TypeOK ==
    /\ recorded \subseteq EVENTS
    /\ valid \subseteq EVENTS
    /\ valid \subseteq recorded \cup {InvitePeerBob}
    /\ (InvitePeerBob \in valid /\ InvitePeerBob \notin recorded) =>
       (InviteAcceptedBob \in valid /\ inviteAcceptedMode = "peer")
    /\ trustAnchor \in {TRUE, FALSE}
    /\ inviteAcceptedMode \in {"none", "user", "peer"}
    /\ connInvite \in {TRUE, FALSE}
    /\ connPeer \in {TRUE, FALSE}

InvDeps ==
    \A e \in valid:
        IF e = InvitePeerBob /\ inviteAcceptedMode = "peer" /\ InviteAcceptedBob \in valid
        THEN TRUE
        ELSE Deps(e) \subseteq valid

InvConnInvite ==
    connInvite => (InviteUserOngoing \in recorded /\ InviteAcceptedBob \in valid)

InvConnPeer ==
    connPeer => (connInvite /\ PeerSharedAlice \in valid /\ PeerSharedBob \in valid)

====
