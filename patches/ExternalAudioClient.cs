
using System;
using System.Collections.Generic;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using Caliburn.Micro;
using Ciribob.DCS.SimpleRadio.Standalone.Common.Models;
using Ciribob.DCS.SimpleRadio.Standalone.Common.Models.EventMessages;
using Ciribob.DCS.SimpleRadio.Standalone.Common.Models.Player;
using Ciribob.DCS.SimpleRadio.Standalone.Common.Network.Client;
using Ciribob.DCS.SimpleRadio.Standalone.Common.Network.Singletons;
using Ciribob.DCS.SimpleRadio.Standalone.ExternalAudioClient.Audio;
using NLog;
using LogManager = NLog.LogManager;
using Timer = Cabhishek.Timers.Timer;

namespace Ciribob.DCS.SimpleRadio.Standalone.ExternalAudioClient.Client;

public class ExternalAudioClient : IHandle<TCPClientStatusMessage>
{
    private static readonly Logger Logger = LogManager.GetCurrentClassLogger();
    private static readonly TimeSpan VoipReadyTimeout = TimeSpan.FromSeconds(20);

    private double[] freq;
    private Modulation[] modulation;
    private byte[] modulationBytes;

    private readonly string Guid = ShortGuid.NewGuid();

    private TaskCompletionSource completedTCS = new();
    private CancellationTokenSource finished;
    private UDPVoiceHandler udpVoiceHandler;
    private Program.Options opts;
    private IPEndPoint endPoint;
    private readonly byte[] encryptionBytes;
    private uint unitId = 100000;
    private List<byte[]> opusBytes;

    public ExternalAudioClient(double[] freq, Modulation[] modulation, Program.Options opts)
    {
        this.unitId = opts.UnitId;
        this.freq = freq;
        this.modulation = modulation;
        this.opts = opts;
        this.modulationBytes = new byte[modulation.Length];
        for (int i = 0; i < modulationBytes.Length; i++)
        {
            modulationBytes[i] = (byte)modulation[i];
        }

        encryptionBytes = new byte[modulation.Length];
        for (var i = 0; i < encryptionBytes.Length; i++) encryptionBytes[i] = 0;

        endPoint = ResolveEndPoint(opts.IP, opts.Port);
        Logger.Info($"Resolved SRS endpoint: {endPoint}");

        EventBus.Instance.SubscribeOnUIThread(this);
    }

    private static IPEndPoint ResolveEndPoint(string host, int port)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            throw new ArgumentException("SRS server IP/hostname is required", nameof(host));
        }

        host = host.Trim();

        // Allow host:port in --ip while still respecting --port if no port embedded
        var hostOnly = host;
        var resolvedPort = port;
        var colonIndex = host.LastIndexOf(':');
        if (colonIndex > 0 && host.IndexOf(']') < 0) // skip IPv6 literals
        {
            var portPart = host.Substring(colonIndex + 1);
            if (int.TryParse(portPart, out var embeddedPort))
            {
                hostOnly = host.Substring(0, colonIndex);
                resolvedPort = embeddedPort;
            }
        }

        if (IPAddress.TryParse(hostOnly, out var parsedIp))
        {
            return new IPEndPoint(parsedIp, resolvedPort);
        }

        var resolvedAddresses = Dns.GetHostAddresses(hostOnly);
        var ipv4 = Array.Find(resolvedAddresses, a => a.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork);
        if (ipv4 == null)
        {
            throw new Exception($"Unable to resolve IPv4 address for SRS host '{hostOnly}'");
        }

        return new IPEndPoint(ipv4, resolvedPort);
    }

    public async Task HandleAsync(TCPClientStatusMessage message, CancellationToken cancellationToken)
    {
        if (message.Connected)
            await ReadyToSendAsync();
        else
            Disconnected();
    }

    public async Task StartAsync()
    {
        // Generate audio BEFORE connecting so TTS/file failures do not leave ghost SRS clients.
        Logger.Info("Generating audio before SRS connect...");
        opusBytes = GenerateOpusOnStaThread();
        if (opusBytes == null || opusBytes.Count == 0)
        {
            Logger.Error("No audio frames generated — not connecting to SRS.");
            return;
        }

        Logger.Info($"Generated {opusBytes.Count} Opus frames ({opusBytes.Count * 40} ms)");

        var radioInfoBase = new PlayerRadioInfoBase();
        radioInfoBase.radios[1].modulation = modulation[0];
        radioInfoBase.radios[1].freq = freq[0]; // get into Hz
        radioInfoBase.unitId = unitId;

        Logger.Info($"Starting with params:");
        for (int i = 0; i < freq.Length; i++)
        {
            Logger.Info($"Frequency: {freq[i]} Hz - {modulation[i]} ");
        }

        LatLngPosition position = new LatLngPosition()
        {
            alt = opts.Altitude,
            lat = opts.Latitude,
            lng = opts.Longitude
        };

        radioInfoBase.ambient = new Ambient()
            { abType = opts.AmbientCockpit.ToLowerInvariant().Trim(), vol = opts.AmbientCockpitVolume };

        var srClient = new SRClientBase
        {
            LatLngPosition = position,
            AllowRecord = opts.Record,
            ClientGuid = Guid,
            Coalition = opts.Coalition,
            Name = opts.Name,
            RadioInfo = radioInfoBase
        };
        var srsClientSyncHandler = new TCPClientHandler(Guid, srClient);

        using (finished = new CancellationTokenSource())
        {
            srsClientSyncHandler.TryConnect(endPoint);

            //wait for it to end
            await completedTCS.Task;
        }

        Logger.Info("Finished - Closing");

        udpVoiceHandler?.RequestStop();
        srsClientSyncHandler?.RequestDisconnectAsync();
    }

    private List<byte[]> GenerateOpusOnStaThread()
    {
        List<byte[]> result = null;
        Exception error = null;

        var thread = new Thread(() =>
        {
            try
            {
                var audioGenerator = new AudioGenerator(opts);
                result = audioGenerator.GetOpusBytes();
            }
            catch (Exception ex)
            {
                error = ex;
            }
        });
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        if (error != null)
        {
            Logger.Error(error, "Audio generation failed");
            return null;
        }

        return result;
    }

    private async Task ReadyToSendAsync()
    {
        if (udpVoiceHandler == null)
        {
            Logger.Info($"Connecting UDP VoIP {endPoint}");
            udpVoiceHandler = new UDPVoiceHandler(Guid, endPoint);
            udpVoiceHandler.Connect();
            _ = Task.Run(async () =>
            {
                try
                {
                    await SendAudioAsync();
                }
                catch (Exception ex)
                {
                    Logger.Error(ex, "SendAudioAsync failed");
                    Disconnected();
                }
            });
        }
    }

    private void Disconnected()
    {
        completedTCS.TrySetResult();
    }

    private async Task SendAudioAsync()
    {
        Logger.Info("Sending Audio... Please Wait");
        var count = 0;
        var frames = opusBytes ?? new List<byte[]>();

        var readyDeadline = DateTime.UtcNow + VoipReadyTimeout;
        while (!udpVoiceHandler.Ready && !finished.IsCancellationRequested)
        {
            if (DateTime.UtcNow > readyDeadline)
            {
                Logger.Error($"UDP VoIP not ready after {VoipReadyTimeout.TotalSeconds:0}s — aborting");
                Disconnected();
                return;
            }

            finished.Token.ThrowIfCancellationRequested();
            await Task.Delay(TimeSpan.FromMilliseconds(100), finished.Token);
        }

        Logger.Info("UDP VoIP ready — transmitting");

        var audioSentTCS = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        uint _packetNumber = 1;

        var _timer = new Timer(() =>
        {
            if (!finished.IsCancellationRequested)
            {
                if (count < frames.Count)
                {
                    var udpVoicePacket = new UDPVoicePacket
                    {
                        AudioPart1Bytes = frames[count],
                        AudioPart1Length = (ushort)frames[count].Length,
                        Frequencies = freq,
                        UnitId = unitId,
                        Encryptions = encryptionBytes,
                        Modulations = modulationBytes,
                        RetransmissionCount = 0,
                        PacketNumber = _packetNumber++
                    };

                    udpVoiceHandler.Send(udpVoicePacket);
                    count++;

                    if (count % 50 == 0)
                        Logger.Info(
                            $"Playing audio - sent {count * 40}ms - {count / (float)frames.Count * 100.0:F0}% ");
                }
                else
                {
                    audioSentTCS.TrySetResult();
                }
            }
            else
            {
                Logger.Error("Client Disconnected");
                audioSentTCS.TrySetCanceled();
            }
        }, TimeSpan.FromMilliseconds(40));
        _timer.Start();

        try
        {
            await audioSentTCS.Task;
        }
        finally
        {
            _timer.Stop();
        }

        Logger.Info("Finished Sending Audio");
        Disconnected();
    }
}
