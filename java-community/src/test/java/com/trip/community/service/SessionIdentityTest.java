package com.trip.community.service;

import static org.junit.jupiter.api.Assertions.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;

class SessionIdentityTest {
    @Test void missingSessionNeverCreatesAUser() {
        var identity=new SessionIdentity("http://127.0.0.1:1");
        assertEquals(0,identity.resolve(null,false));
        assertEquals(401,assertThrows(ResponseStatusException.class,()->identity.resolve(null,true)).getStatusCode().value());
    }

    @Test void verifiedSessionControlsIdentityAndRevocation() throws Exception {
        var server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        server.createContext("/api/auth/community",exchange->{
            boolean valid="valid-token".equals(exchange.getRequestHeaders().getFirst("X-Session-Token"));
            boolean throttled="throttled".equals(exchange.getRequestHeaders().getFirst("X-Session-Token"));
            byte[] body=(valid?"{\"userId\":42}":"{}").getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type","application/json");
            exchange.sendResponseHeaders(valid?200:throttled?429:401,body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            var identity=new SessionIdentity("http://127.0.0.1:"+server.getAddress().getPort());
            assertEquals(42,identity.resolve("valid-token",true));
            assertEquals(401,assertThrows(ResponseStatusException.class,()->identity.resolve("revoked",true)).getStatusCode().value());
            assertEquals(429,assertThrows(ResponseStatusException.class,()->identity.resolve("throttled",true)).getStatusCode().value());
        } finally { server.stop(0); }
    }
}
