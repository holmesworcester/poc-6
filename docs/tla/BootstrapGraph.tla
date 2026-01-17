---- MODULE BootstrapGraph ----
EXTENDS Naturals

\* Model of the user-auth invite chain and bootstrap connection upgrade.
\* This models a single peer's view (joiner/Bob) where network validity is
\* gated by a local trust anchor (invite_accepted).
\*
\* Sources:
\* - docs/planning/network-root-linking-design.md
\* - tests/v2_projectors/test_identity_chain.py

VARIABLES recorded, valid, trustAnchor, connInvite, connPeer

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
       [] e = AdminGrantAlice -> {Net, UserAlice}
       [] e = InvitePeerAlice -> {UserAlice}
       [] e = PeerSharedAlice -> {InvitePeerAlice}
       [] e = InviteUserOngoing -> {PeerSharedAlice, AdminGrantAlice}
       [] e = InviteAcceptedBob -> {}
       [] e = UserBob -> {InviteUserOngoing}
       [] e = InvitePeerBob -> {UserBob}
       [] e = PeerSharedBob -> {InvitePeerBob}
       [] OTHER -> {}

Guard(e) ==
    IF e = Net THEN trustAnchor ELSE TRUE

Init ==
    /\ recorded = {}
    /\ valid = {}
    /\ trustAnchor = FALSE
    /\ connInvite = FALSE
    /\ connPeer = FALSE

Record(e) ==
    /\ e \in EVENTS
    /\ e \notin recorded
    /\ recorded' = recorded \cup {e}
    /\ UNCHANGED <<valid, trustAnchor, connInvite, connPeer>>

Project(e) ==
    /\ e \in recorded
    /\ e \notin valid
    /\ Deps(e) \subseteq valid
    /\ Guard(e)
    /\ valid' = valid \cup {e}
    /\ trustAnchor' = IF e = InviteAcceptedBob THEN TRUE ELSE trustAnchor
    /\ UNCHANGED <<recorded, connInvite, connPeer>>

\* Bootstrap connection: invite-labeled connection before peer_shared is known.
ConnectByInvite ==
    /\ ~connInvite
    /\ InviteUserOngoing \in valid
    /\ InviteAcceptedBob \in valid
    /\ connInvite' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connPeer>>

\* Upgrade to peer_shared-labeled connection once both peers are known.
UpgradeToPeer ==
    /\ connInvite
    /\ ~connPeer
    /\ PeerSharedAlice \in valid
    /\ PeerSharedBob \in valid
    /\ connPeer' = TRUE
    /\ UNCHANGED <<recorded, valid, trustAnchor, connInvite>>

Stutter ==
    UNCHANGED <<recorded, valid, trustAnchor, connInvite, connPeer>>

Next ==
    \/ \E e \in EVENTS: Record(e)
    \/ \E e \in EVENTS: Project(e)
    \/ ConnectByInvite
    \/ UpgradeToPeer
    \/ Stutter

Spec ==
    Init /\ [][Next]_<<recorded, valid, trustAnchor, connInvite, connPeer>>

TypeOK ==
    /\ recorded \subseteq EVENTS
    /\ valid \subseteq EVENTS
    /\ valid \subseteq recorded
    /\ trustAnchor \in {TRUE, FALSE}
    /\ connInvite \in {TRUE, FALSE}
    /\ connPeer \in {TRUE, FALSE}

InvDeps ==
    \A e \in valid: Deps(e) \subseteq valid

InvNetworkTrust ==
    Net \in valid => trustAnchor

InvConnInvite ==
    connInvite => (InviteUserOngoing \in valid /\ InviteAcceptedBob \in valid)

InvConnPeer ==
    connPeer => (connInvite /\ PeerSharedAlice \in valid /\ PeerSharedBob \in valid)

====
