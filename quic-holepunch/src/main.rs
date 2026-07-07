//! QUIC hole-punch peer (peer-to-peer intro).
//!
//! Every peer can both connect to others and accept connections. When a peer
//! has connections to 2+ other peers, it sends intro messages telling each
//! about the other — no separate intro server needed.
//!
//! Usage:
//!   # Charlie (introducer, directly reachable):
//!   quic-holepunch --peer-id charlie --bind 0.0.0.0:4433
//!
//!   # Alice (behind NAT, connects to charlie):
//!   quic-holepunch --peer-id alice --bind 0.0.0.0:9000 --connect 10.10.0.1:4433
//!
//!   # Bob (behind NAT, connects to charlie):
//!   quic-holepunch --peer-id bob --bind 0.0.0.0:9000 --connect 10.20.0.1:4433
//!
//! Charlie accepts hello from Alice and Bob, then sends intro messages so they
//! can hole-punch directly.

use anyhow::{Context, Result};
use quinn::Endpoint;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use serde::{Deserialize, Serialize};
use socket2::{Domain, Protocol, Socket, Type};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::Mutex;
use tracing::{error, info, warn};

const PUNCH_ATTEMPTS: usize = 5;
const ATTEMPT_INTERVAL: Duration = Duration::from_millis(200);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

// ---------------------------------------------------------------------------
// Protocol messages (JSON lines over QUIC bidi stream)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
enum Message {
    #[serde(rename = "hello")]
    Hello {
        peer_id: String,
        holepunch_port: u16,
    },
    #[serde(rename = "welcome")]
    Welcome {
        peer_id: String,
        observed_addr: String,
    },
    #[serde(rename = "intro")]
    Intro { peer_id: String, addr: String },
}

// ---------------------------------------------------------------------------
// Peer tracking
// ---------------------------------------------------------------------------

struct PeerEntry {
    /// Address to use in intro messages: observed IP + declared holepunch port.
    holepunch_addr: SocketAddr,
    send: quinn::SendStream,
}

type PeerMap = Arc<Mutex<HashMap<String, PeerEntry>>>;

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct Args {
    peer_id: String,
    bind_addr: SocketAddr,
    connect_addrs: Vec<SocketAddr>,
}

fn parse_args() -> Result<Args> {
    let mut args = std::env::args().skip(1);
    let mut peer_id = None;
    let mut bind_addr: SocketAddr = "0.0.0.0:9000".parse().unwrap();
    let mut connect_addrs = Vec::new();

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--peer-id" => peer_id = Some(args.next().context("--peer-id requires value")?),
            "--bind" => {
                bind_addr = args
                    .next()
                    .context("--bind requires value")?
                    .parse()
                    .context("invalid bind address")?
            }
            "--connect" => {
                let addr: SocketAddr = args
                    .next()
                    .context("--connect requires value")?
                    .parse()
                    .context("invalid connect address")?;
                connect_addrs.push(addr);
            }
            other => anyhow::bail!("unknown argument: {other}"),
        }
    }

    Ok(Args {
        peer_id: peer_id.context("--peer-id required")?,
        bind_addr,
        connect_addrs,
    })
}

// ---------------------------------------------------------------------------
// TLS helpers
// ---------------------------------------------------------------------------

fn gen_self_signed_cert() -> Result<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)> {
    let cert = rcgen::generate_simple_self_signed(vec!["localhost".into()])?;
    let key = PrivatePkcs8KeyDer::from(cert.key_pair.serialize_der());
    let cert_der = CertificateDer::from(cert.cert);
    Ok((vec![cert_der], PrivateKeyDer::Pkcs8(key)))
}

/// Skip TLS certificate verification (peers use self-signed certs,
/// authentication happens at the application layer via peer IDs).
#[derive(Debug)]
struct SkipVerify;

impl rustls::client::danger::ServerCertVerifier for SkipVerify {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &rustls::pki_types::ServerName<'_>,
        _ocsp_response: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> std::result::Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}

fn make_client_config() -> Result<quinn::ClientConfig> {
    let client_crypto = rustls::ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(SkipVerify))
        .with_no_client_auth();
    Ok(quinn::ClientConfig::new(Arc::new(
        quinn::crypto::rustls::QuicClientConfig::try_from(client_crypto)?,
    )))
}

// ---------------------------------------------------------------------------
// Endpoint construction
// ---------------------------------------------------------------------------

/// Create a quinn Endpoint that can both connect (client) and accept (server)
/// on the same UDP socket.
fn make_dual_endpoint(bind_addr: SocketAddr) -> Result<Endpoint> {
    let (certs, key) = gen_self_signed_cert()?;

    let server_crypto = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    let server_config = quinn::ServerConfig::with_crypto(Arc::new(
        quinn::crypto::rustls::QuicServerConfig::try_from(server_crypto)?,
    ));

    let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    socket.set_reuse_address(true)?;
    socket.set_nonblocking(true)?;
    socket.bind(&bind_addr.into())?;
    let std_socket: std::net::UdpSocket = socket.into();

    let runtime = quinn::default_runtime().context("no async runtime")?;
    let mut endpoint = Endpoint::new(
        quinn::EndpointConfig::default(),
        Some(server_config),
        std_socket,
        runtime,
    )?;

    endpoint.set_default_client_config(make_client_config()?);

    info!("endpoint bound to {}", endpoint.local_addr()?);
    Ok(endpoint)
}

// ---------------------------------------------------------------------------
// Wire helpers
// ---------------------------------------------------------------------------

async fn send_msg(send: &mut quinn::SendStream, msg: &Message) -> Result<()> {
    let mut json = serde_json::to_string(msg)?;
    json.push('\n');
    send.write_all(json.as_bytes()).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Accepting connections (intro-server role merged into every peer)
// ---------------------------------------------------------------------------

/// Accept incoming QUIC connections and handle hello/intro protocol.
async fn accept_loop(endpoint: Endpoint, peers: PeerMap, my_peer_id: String, hp_endpoint: Endpoint) {
    loop {
        let incoming = match endpoint.accept().await {
            Some(inc) => inc,
            None => {
                info!("accept_loop: endpoint closed");
                return;
            }
        };
        let peers = peers.clone();
        let my_peer_id = my_peer_id.clone();
        let hp_endpoint = hp_endpoint.clone();
        tokio::spawn(async move {
            if let Err(e) = handle_peer(incoming, peers, &my_peer_id, &hp_endpoint).await {
                warn!("handle_peer error: {e:#}");
            }
        });
    }
}

/// Handle an incoming peer connection: read Hello, send Welcome, store in
/// peer map, and send Intro messages when we know 2+ peers.
async fn handle_peer(
    incoming: quinn::Incoming,
    peers: PeerMap,
    my_peer_id: &str,
    hp_endpoint: &Endpoint,
) -> Result<()> {
    let remote_addr = incoming.remote_address();
    let conn = incoming.await?;
    info!("incoming connection from {remote_addr}");

    let (send, recv) = conn.accept_bi().await?;
    let mut reader = BufReader::new(recv);
    let mut line = String::new();

    // Read Hello
    reader.read_line(&mut line).await?;
    let msg: Message = serde_json::from_str(line.trim())?;

    let (peer_id, holepunch_port) = match msg {
        Message::Hello {
            peer_id,
            holepunch_port,
        } => (peer_id, holepunch_port),
        _ => anyhow::bail!("expected hello message, got: {line}"),
    };

    // Combine observed IP with declared holepunch port
    let holepunch_addr = SocketAddr::new(remote_addr.ip(), holepunch_port);
    info!(
        "peer {peer_id} said hello: connection from {remote_addr}, \
         holepunch addr = {holepunch_addr}"
    );

    // Send Welcome
    let welcome = Message::Welcome {
        peer_id: peer_id.clone(),
        observed_addr: holepunch_addr.to_string(),
    };
    let mut send_stream = send;
    send_msg(&mut send_stream, &welcome).await?;

    // Store peer and send intros if we have 2+
    {
        let mut map = peers.lock().await;
        map.insert(
            peer_id.clone(),
            PeerEntry {
                holepunch_addr,
                send: send_stream,
            },
        );

        if map.len() >= 2 {
            let peer_ids: Vec<String> = map.keys().cloned().collect();
            for other_id in &peer_ids {
                if other_id == &peer_id {
                    continue;
                }

                let other_addr = map.get(other_id).unwrap().holepunch_addr;
                let this_addr = holepunch_addr;

                // Tell the new peer about the existing peer
                let intro_to_new = Message::Intro {
                    peer_id: other_id.clone(),
                    addr: other_addr.to_string(),
                };
                if let Some(entry) = map.get_mut(&peer_id) {
                    let _ = send_msg(&mut entry.send, &intro_to_new).await;
                }

                // Tell the existing peer about the new peer
                let intro_to_existing = Message::Intro {
                    peer_id: peer_id.clone(),
                    addr: this_addr.to_string(),
                };
                if let Some(entry) = map.get_mut(other_id) {
                    let _ = send_msg(&mut entry.send, &intro_to_existing).await;
                }

                info!("sent intros: {peer_id} <-> {other_id} ({this_addr} <-> {other_addr})");
            }
        }
    }

    // Keep connection alive; also handle any messages (e.g. if this peer
    // receives intros from the remote — though normally only the connector
    // side reads intros).
    let mut buf = String::new();
    loop {
        buf.clear();
        match reader.read_line(&mut buf).await {
            Ok(0) => break,
            Ok(_) => {
                // Could receive intro messages if the remote peer also acts as introducer
                if let Ok(msg) = serde_json::from_str::<Message>(buf.trim()) {
                    if let Message::Intro { peer_id, addr } = msg {
                        info!("received intro (on accepted conn) for peer {peer_id} at {addr}");
                        if let Ok(their_addr) = addr.parse::<SocketAddr>() {
                            let ep = hp_endpoint.clone();
                            let my_id = my_peer_id.to_string();
                            let their_id = peer_id.clone();
                            tokio::spawn(async move {
                                spawn_holepunch(&ep, their_addr, &their_id, &my_id).await;
                            });
                        }
                    }
                }
            }
            Err(_) => break,
        }
    }

    peers.lock().await.remove(&peer_id);
    info!("peer {peer_id} disconnected");

    Ok(())
}

// ---------------------------------------------------------------------------
// Outbound connection (connecting to a known peer)
// ---------------------------------------------------------------------------

/// Connect to a known peer via a SEPARATE ephemeral endpoint.
///
/// Why separate? Linux conntrack is endpoint-dependent: if the hole-punch
/// endpoint (port 9000) connects to the peer, the NAT creates a conntrack
/// entry for (internal:9000 -> peer:4433). When the peer later sends
/// hole-punch packets to a different destination, the NAT gives it a
/// DIFFERENT external port. By using a separate endpoint, port 9000 stays
/// clean for the hole-punch.
async fn connect_to_peer(
    peer_addr: SocketAddr,
    my_peer_id: &str,
    holepunch_port: u16,
    hp_endpoint: Endpoint,
) -> Result<()> {
    let mut endpoint = Endpoint::client("0.0.0.0:0".parse()?)?;
    endpoint.set_default_client_config(make_client_config()?);

    info!(
        "connecting to peer at {peer_addr} (separate endpoint, holepunch_port={holepunch_port})"
    );
    let conn = endpoint.connect(peer_addr, "localhost")?.await?;
    info!("connected to peer at {peer_addr}");

    let (mut send, recv) = conn.open_bi().await?;
    let mut reader = BufReader::new(recv);

    // Send Hello
    let hello = Message::Hello {
        peer_id: my_peer_id.to_string(),
        holepunch_port,
    };
    send_msg(&mut send, &hello).await?;

    // Read Welcome + Intro messages
    let mut line = String::new();
    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            info!("peer at {peer_addr} disconnected");
            break;
        }

        let msg: Message = serde_json::from_str(line.trim())?;
        match msg {
            Message::Welcome {
                peer_id,
                observed_addr,
            } => {
                info!("welcome from peer: registered as {peer_id}, observed at {observed_addr}");
            }
            Message::Intro { peer_id, addr } => {
                info!("received intro for peer {peer_id} at {addr}");
                let their_addr: SocketAddr = addr.parse()?;
                let ep = hp_endpoint.clone();
                let my_id = my_peer_id.to_string();
                let their_id = peer_id.clone();

                tokio::spawn(async move {
                    spawn_holepunch(&ep, their_addr, &their_id, &my_id).await;
                });
            }
            _ => {}
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Hole-punch logic (unchanged from original)
// ---------------------------------------------------------------------------

async fn spawn_holepunch(
    endpoint: &Endpoint,
    their_addr: SocketAddr,
    their_peer_id: &str,
    my_peer_id: &str,
) {
    match hole_punch_race(endpoint, their_addr, their_peer_id, my_peer_id).await {
        Ok(conn) => {
            info!(
                "HOLE PUNCH SUCCESS with {their_peer_id}! \
                 remote={}, local_ip={:?}",
                conn.remote_address(),
                conn.local_ip(),
            );
            if let Err(e) = verify_connection(&conn, my_peer_id).await {
                error!("connection verification failed: {e:#}");
            } else {
                info!("connection verified - data exchange working!");
            }
        }
        Err(e) => {
            error!("HOLE PUNCH FAILED with {their_peer_id}: {e:#}");
        }
    }
}

/// The hole-punch race: simultaneously try to connect as client AND accept as
/// server. Both peers do this. First successful connection wins.
async fn hole_punch_race(
    endpoint: &Endpoint,
    their_addr: SocketAddr,
    their_peer_id: &str,
    _my_peer_id: &str,
) -> Result<quinn::Connection> {
    info!(
        "starting hole-punch race with {their_peer_id} at {their_addr} \
         (my port: {})",
        endpoint.local_addr()?.port()
    );

    let connect_fut = {
        let endpoint = endpoint.clone();
        let their_peer_id = their_peer_id.to_string();
        async move { punch_connect(&endpoint, their_addr, &their_peer_id).await }
    };

    let accept_fut = {
        let endpoint = endpoint.clone();
        let their_peer_id = their_peer_id.to_string();
        async move { punch_accept(&endpoint, &their_peer_id).await }
    };

    tokio::pin!(connect_fut);
    tokio::pin!(accept_fut);

    let mut connect_done = false;
    let mut accept_done = false;
    let mut connect_err: Option<anyhow::Error> = None;
    let mut accept_err: Option<anyhow::Error> = None;

    loop {
        tokio::select! {
            result = &mut connect_fut, if !connect_done => {
                connect_done = true;
                match result {
                    Ok(conn) => {
                        info!("hole-punch succeeded via CONNECT (I'm the client)");
                        return Ok(conn);
                    }
                    Err(e) => {
                        warn!("all connect attempts failed: {e:#}");
                        connect_err = Some(e);
                    }
                }
            }
            result = &mut accept_fut, if !accept_done => {
                accept_done = true;
                match result {
                    Ok(conn) => {
                        info!("hole-punch succeeded via ACCEPT (I'm the server)");
                        return Ok(conn);
                    }
                    Err(e) => {
                        warn!("accept failed: {e:#}");
                        accept_err = Some(e);
                    }
                }
            }
        }

        if connect_done && accept_done {
            anyhow::bail!(
                "hole-punch failed: connect={}, accept={}",
                connect_err
                    .as_ref()
                    .map(|e| format!("{e:#}"))
                    .unwrap_or_default(),
                accept_err
                    .as_ref()
                    .map(|e| format!("{e:#}"))
                    .unwrap_or_default(),
            );
        }
    }
}

/// Try multiple QUIC connect() attempts to the peer's observed address.
async fn punch_connect(
    endpoint: &Endpoint,
    their_addr: SocketAddr,
    their_peer_id: &str,
) -> Result<quinn::Connection> {
    let mut last_err = None;

    for attempt in 1..=PUNCH_ATTEMPTS {
        info!("connect attempt {attempt}/{PUNCH_ATTEMPTS} to {their_peer_id} at {their_addr}");

        let connecting = match endpoint.connect(their_addr, "localhost") {
            Ok(c) => c,
            Err(e) => {
                warn!("connect attempt {attempt} endpoint error: {e}");
                last_err = Some(anyhow::Error::from(e));
                tokio::time::sleep(ATTEMPT_INTERVAL).await;
                continue;
            }
        };

        match tokio::time::timeout(CONNECT_TIMEOUT, connecting).await {
            Ok(Ok(conn)) => {
                info!("connect attempt {attempt} succeeded!");
                return Ok(conn);
            }
            Ok(Err(e)) => {
                warn!("connect attempt {attempt} handshake failed: {e}");
                last_err = Some(anyhow::Error::from(e));
            }
            Err(_) => {
                warn!("connect attempt {attempt} timed out");
                last_err = Some(anyhow::anyhow!("timeout"));
            }
        }

        tokio::time::sleep(ATTEMPT_INTERVAL).await;
    }

    Err(last_err.unwrap_or_else(|| anyhow::anyhow!("no attempts made")))
}

/// Accept an incoming QUIC connection from the peer.
async fn punch_accept(endpoint: &Endpoint, their_peer_id: &str) -> Result<quinn::Connection> {
    info!("waiting for incoming connection from {their_peer_id}");

    let timeout = Duration::from_secs(PUNCH_ATTEMPTS as u64 * 2);
    match tokio::time::timeout(timeout, async {
        loop {
            match endpoint.accept().await {
                Some(incoming) => {
                    let remote = incoming.remote_address();
                    info!("incoming connection from {remote}");
                    match incoming.await {
                        Ok(conn) => return Ok(conn),
                        Err(e) => {
                            warn!("incoming handshake failed: {e}");
                            continue;
                        }
                    }
                }
                None => return Err(anyhow::anyhow!("endpoint closed")),
            }
        }
    })
    .await
    {
        Ok(result) => result,
        Err(_) => Err(anyhow::anyhow!("accept timed out after {timeout:?}")),
    }
}

/// After hole-punch succeeds, verify we can actually exchange data.
async fn verify_connection(conn: &quinn::Connection, my_peer_id: &str) -> Result<()> {
    tokio::select! {
        r = async {
            let (mut send, mut recv) = conn.open_bi().await?;
            send.write_all(my_peer_id.as_bytes()).await?;
            send.finish()?;
            let mut buf = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut recv, &mut buf).await?;
            let their_id = String::from_utf8(buf)?;
            info!("verified connection: they say they are '{their_id}'");
            Ok::<_, anyhow::Error>(())
        } => r,
        r = async {
            let (mut send, mut recv) = conn.accept_bi().await?;
            let mut buf = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut recv, &mut buf).await?;
            let their_id = String::from_utf8(buf)?;
            info!("verified connection: they say they are '{their_id}'");
            send.write_all(my_peer_id.as_bytes()).await?;
            send.finish()?;
            Ok::<_, anyhow::Error>(())
        } => r,
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = parse_args()?;
    info!("peer {} starting", args.peer_id);

    // Hole-punch endpoint (dual: client + server on same UDP socket)
    let hp_endpoint = make_dual_endpoint(args.bind_addr)?;
    let local_addr = hp_endpoint.local_addr()?;
    info!("hole-punch endpoint on {local_addr}");

    // Peer map for tracking connected peers (intro-server role)
    let peers: PeerMap = Arc::new(Mutex::new(HashMap::new()));

    // Spawn accept loop — every peer can receive connections
    let accept_ep = hp_endpoint.clone();
    let accept_peers = peers.clone();
    let accept_id = args.peer_id.clone();
    let accept_hp = hp_endpoint.clone();
    tokio::spawn(async move {
        accept_loop(accept_ep, accept_peers, accept_id, accept_hp).await;
    });

    // Connect to known peers (if any)
    if args.connect_addrs.is_empty() {
        info!("no --connect specified, listening for incoming connections only");
        // Just wait forever
        std::future::pending::<()>().await;
    } else {
        let mut handles = Vec::new();
        for peer_addr in &args.connect_addrs {
            let addr = *peer_addr;
            let my_id = args.peer_id.clone();
            let hp = hp_endpoint.clone();
            let port = local_addr.port();
            handles.push(tokio::spawn(async move {
                if let Err(e) = connect_to_peer(addr, &my_id, port, hp).await {
                    error!("connection to {addr} failed: {e:#}");
                }
            }));
        }

        // Wait for all outbound connections to finish
        for h in handles {
            let _ = h.await;
        }
    }

    Ok(())
}
