---- MODULE BootstrapGraph ----
EXTENDS Naturals

\* Model of the user-auth invite chain and bootstrap connection upgrade.
\* Focuses on v2 projection semantics (EVENT_SPEC deps + signer gating),
\* plus trust-anchored bootstrap: network validity is gated by invite_accepted
\* for *all* cases (including self-invite in bootstrap).
\*
\* Sources:
\* - docs/planning/network-root-linking-design.md
\* - tests/projectors/test_identity_chain.py

VARIABLES recorded, valid, trustAnchor, connReq, connAck, connInvite, connPeer

Net == "network"
InviteAcceptedAlice == "invite_accepted_alice"
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
    Net, InviteAcceptedAlice, InviteUserBoot, UserAlice, AdminGrantAlice,
    InvitePeerAlice, PeerSharedAlice, InviteUserOngoing, InviteAcceptedBob,
    UserBob, InvitePeerBob, PeerSharedBob
}

Deps(e) ==
    CASE e = Net -> {}
       [] e = InviteAcceptedAlice -> {}
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

\* Network is only valid after an invite_accepted trust anchor.
Guard(e) == IF e = Net THEN trustAnchor ELSE TRUE

Init ==
    /\ recorded = {}
    /\ valid = {}
    /\ trustAnchor = FALSE
    /\ connReq = FALSE
    /\ connAck = FALSE
    /\ connInvite = FALSE
    /\ connPeer = FALSE

Record(e) ==
    /\ e \in EVENTS
    /\ e \notin recorded
    /\ recorded' = recorded \cup {e}
    /\ UNCHANGED <<valid, trustAnchor, connReq, connAck, connInvite, connPeer>>

Project(e) ==
    /\ e \in recorded
    /\ e \notin valid
    /\ Deps(e) \subseteq valid
    /\ Guard(e)
    /\ valid' = valid \cup {e}
    /\ trustAnchor' =
        IF e \in {InviteAcceptedAlice, InviteAcceptedBob} THEN TRUE ELSE trustAnchor
    /\ UNCHANGED <<recorded, connReq, connAck, connInvite, connPeer>>

\* Bootstrap connection request: authenticated by invite signature.
ConnectReqByInvite ==
    /\ ~connReq
    /\ InviteAcceptedBob \in valid
    /\ InviteUserOngoing \in recorded
    /\ connReq' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connAck, connInvite, connPeer>>

\* Connection acknowledgment: only after request is accepted.
ConnectAck ==
    /\ connReq
    /\ ~connAck
    /\ connAck' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connReq, connInvite, connPeer>>

\* Bootstrap connection active (invite-labeled) after ack.
ConnectByInvite ==
    /\ ~connInvite
    /\ connAck
    /\ connInvite' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connReq, connAck, connPeer>>

\* Upgrade to peer_shared-labeled connection once both peers are known.
UpgradeToPeer ==
    /\ connInvite
    /\ ~connPeer
    /\ PeerSharedAlice \in valid
    /\ PeerSharedBob \in valid
    /\ connPeer' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connReq, connAck, connInvite>>

Stutter ==
    UNCHANGED <<recorded, valid, trustAnchor, connReq, connAck, connInvite, connPeer>>

Next ==
    \/ \E e \in EVENTS: Record(e)
    \/ \E e \in EVENTS: Project(e)
    \/ ConnectReqByInvite
    \/ ConnectAck
    \/ ConnectByInvite
    \/ UpgradeToPeer
    \/ Stutter

Spec ==
    Init /\ [][Next]_<<recorded, valid, trustAnchor, connReq, connAck, connInvite, connPeer>>

TypeOK ==
    /\ recorded \subseteq EVENTS
    /\ valid \subseteq EVENTS
    /\ valid \subseteq recorded
    /\ trustAnchor \in {TRUE, FALSE}
    /\ connReq \in {TRUE, FALSE}
    /\ connAck \in {TRUE, FALSE}
    /\ connInvite \in {TRUE, FALSE}
    /\ connPeer \in {TRUE, FALSE}

InvDeps ==
    \A e \in valid:
        Deps(e) \subseteq valid

InvNetAnchor ==
    Net \in valid => trustAnchor

InvConnReq ==
    connReq => (InviteAcceptedBob \in valid /\ InviteUserOngoing \in recorded)

InvConnAck ==
    connAck => connReq

InvConnInvite ==
    connInvite => (connAck /\ InviteUserOngoing \in recorded /\ InviteAcceptedBob \in valid)

InvConnPeer ==
    connPeer => (connInvite /\ PeerSharedAlice \in valid /\ PeerSharedBob \in valid)

====
